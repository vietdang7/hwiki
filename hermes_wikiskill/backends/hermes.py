"""Hermes backend: isolated per-workspace HERMES_HOME, minimal refreshed credentials,
feature-detected argv, and a skills-visibility proof after EVERY rollout.

Upstream defects fixed here (research/ANALYSIS.md):
- D1/D17: every role runs with the ``skills`` toolset, the workspace config never
  disables it, and the exported ``system_prompt`` must list every expected skill in
  ``<available_skills>`` — otherwise ``SkillsInvisibleError`` (exit 3), not a score.
- D3: ``dry_run`` builds the argv only; it writes nothing (no query file, no symlinks).
- D5: credentials = the model provider's key only, refreshed on every run, 0600.
- D6: argv from ``compat`` (only flags the installed Hermes has).
- D7: launch failures (argparse exit 2, interrupted, no session, never ran) raise
  ``RolloutError`` instead of being graded as 0.0.
- Q4: the ``-Q`` session id is read from STDERR.
- H1/H2: every hermes subprocess gets an explicit non-default HERMES_HOME.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from .. import compat, credentials, sessions, signals
from ..errors import HermesIncompatibleError, RolloutError, SkillsInvisibleError, UnsafePathError
from ..locking import rollout_lock
from ..skillfiles import skill_name
from ..util import atomic_write_json, atomic_write_text, read_json
from .base import FRAMEWORK_SKILLS, RunResult  # noqa: F401

PROFILE_DIR = ".hermes-home"
SKILLS_TOOLSET = "skills"
DEFAULT_TOOLSETS = "terminal,file,code_execution,skills"
MAINTAINER_TOOLSETS = "file,terminal,skills"
PROPOSER_TOOLSETS = "file,terminal,skills"
WORKSPACE_FILE = "workspace.json"
PROFILE_SUBDIRS = ("sessions", "skills", "memories", "logs")


def profile_dir(ws: str) -> str:
    return os.path.join(ws, PROFILE_DIR)


def with_skills(toolsets: str | None) -> str:
    names = [t.strip() for t in (toolsets or DEFAULT_TOOLSETS).split(",") if t.strip()]
    if SKILLS_TOOLSET not in names:
        names.append(SKILLS_TOOLSET)
    return ",".join(names)


# QA-F3: a hermes child never inherits the caller's environment (Hermes' own scrub only
# knows REGISTERED secret names; a caller's custom *_TOKEN / *_API_KEY would reach the
# agent's terminal). Allowlist: locale/terminal/process basics + network plumbing
# (proxies, CA bundles); the provider credential comes from <home>/.env only.
ENV_ALLOW = ("PATH", "LANG", "LANGUAGE", "TZ", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "no_proxy", "all_proxy",
             "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
ENV_ALLOW_PREFIX = ("LC_",)
PASS_ENV = "HWS_PASS_ENV"  # extra names / NAME* prefixes to pass (test doubles only)
TESTING_ENV = "HWS_TESTING"
_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", re.I)


def pass_env_patterns() -> list[str]:
    """R1-19: HWS_PASS_ENV is honoured only with HWS_TESTING=1; a bare ``*`` or a
    secret-looking name/prefix is never passed."""
    if os.environ.get(TESTING_ENV) != "1":
        return []
    out = []
    for p in (x.strip() for x in os.environ.get(PASS_ENV, "").split(",")):
        if not p or p.strip("*") == "" or _SECRETISH.search(p):
            continue
        out.append(p)
    return out


def _passes(name: str, extra: list[str]) -> bool:
    if name in ENV_ALLOW or name.startswith(ENV_ALLOW_PREFIX):
        return True
    return any(name == p or (p.endswith("*") and name.startswith(p[:-1])) for p in extra)


def home_env(home: str, *, isolated: bool = False) -> dict:
    """Environment of a hermes child: allowlisted caller vars + HERMES_HOME + the provider
    vars of ``<home>/.env``. ``isolated`` (task rollouts, QA-F2) also points HOME at the
    rollout home, so ``~`` never reaches the operator's ~/ops tree."""
    extra = pass_env_patterns()
    env = {k: v for k, v in os.environ.items() if _passes(k, extra)}
    home = os.path.abspath(home)
    env.update(credentials.read_dotenv(os.path.join(home, ".env")))
    env["HERMES_HOME"] = home
    if isolated:
        env["HOME"] = home
    elif os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def ws_settings(ws: str) -> dict:
    d = read_json(os.path.join(ws, WORKSPACE_FILE), {})
    return d if isinstance(d, dict) else {}


def save_ws_settings(ws: str, **updates) -> dict:
    d = ws_settings(ws)
    d.setdefault("backend", "hermes")
    d.update({k: v for k, v in updates.items() if v is not None})
    atomic_write_json(os.path.join(ws, WORKSPACE_FILE), d)
    return d


def profile_plan(ws: str) -> dict:
    s = ws_settings(ws)
    src = credentials.source_home(os.environ.get("HWS_SOURCE_PROFILE") or s.get("source_profile"))
    return credentials.plan_profile(src, model=s.get("model"), provider=s.get("provider"))


def refresh_profile(ws: str, home: str | None = None, *, creds: bool = True) -> dict:
    """(Re)write the isolated profile: dirs, opt-out marker, minimal creds (every run, D5).

    ``creds=False`` (the ws's own ``.hermes-home`` at init, R1-17): no provider key is
    written — every real run gets a fresh temporary home with its own minimal .env."""
    home = home or profile_dir(ws)
    for d in PROFILE_SUBDIRS:
        os.makedirs(os.path.join(home, d), exist_ok=True)
    marker = os.path.join(home, ".no-bundled-skills")
    if not os.path.exists(marker):
        atomic_write_text(marker, "hermes-wikiskill: exactly the active skill set\n")
    plan = profile_plan(ws)
    if creds:
        changed = credentials.write_profile(home, plan)
    else:
        changed = credentials.write_profile(home, {**plan, "env": {}})
        env_path = os.path.join(home, ".env")
        if os.path.exists(env_path):
            os.remove(env_path)
            changed = True
    return {"home": home, "provider": plan["provider"], "env_names": plan["env_names"],
            "missing": plan["missing"], "changed": changed}


bootstrap_profile = refresh_profile


def link_skills(home: str, skill_dirs: list[str], *, copy: bool = False) -> None:
    """Make ``<home>/skills`` contain EXACTLY ``skill_dirs`` (symlinks, or copies with ``copy``)."""
    root = os.path.join(home, "skills")
    os.makedirs(root, exist_ok=True)
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if os.path.islink(p) or os.path.isfile(p):
            os.unlink(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
    for src in skill_dirs:
        dst = os.path.join(root, os.path.basename(os.path.normpath(src)))
        if copy:  # D19: isolated task rollouts never point back into the workspace
            copy_skill_dir(src, dst)
        else:
            os.symlink(os.path.abspath(src), dst)


def skill_symlinks(src: str) -> list[str]:
    """Symlinks inside a skill dir (relative paths). R1-16: a copied skill must never
    dereference a link (e.g. to ~/.hermes/.env) into a rollout/eval/role home."""
    out = []
    for dirpath, dirnames, files in os.walk(src):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for n in dirnames + files:
            if os.path.islink(os.path.join(dirpath, n)):
                out.append(os.path.relpath(os.path.join(dirpath, n), src))
    return sorted(out)


def copy_skill_dir(src: str, dst: str) -> None:
    """Copy a skill dir byte-for-byte (``symlinks=True``); refuse one that contains links."""
    links = skill_symlinks(src)
    if links or os.path.islink(src):
        raise UnsafePathError(f"skill {os.path.basename(os.path.normpath(src))!r} contains symlinks "
                              f"{links or ['.']} — refused (R1-16)")
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def active_skill_dirs(ws: str, include_framework: bool = False) -> list[str]:
    out = []
    active = os.path.join(ws, "skills", "active")
    if os.path.isdir(active):
        out += [os.path.join(active, n) for n in sorted(os.listdir(active))
                if not n.startswith(".") and os.path.isfile(os.path.join(active, n, "SKILL.md"))]
    if include_framework:
        fw = os.path.join(ws, "skills", "framework")
        out += [os.path.join(fw, n) for n in FRAMEWORK_SKILLS
                if os.path.isfile(os.path.join(fw, n, "SKILL.md"))]
    return out


def _list_newest_session(binary: str, home: str) -> str | None:
    p = subprocess.run([binary, "sessions", "list"], env=home_env(home),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
    for line in p.stdout.splitlines():
        s = line.strip()
        if s and not s.startswith("Title") and "─" not in s:
            return s.split()[-1]  # list is newest-first
    return None


def export_session(binary: str, home: str, dest: str, session_id: str | None,
                   since: float | None = None) -> str | None:
    """Export by explicit id (from stderr); fallbacks: newest listed, legacy sessions/*.jsonl."""
    sid = session_id or _list_newest_session(binary, home)
    if sid:
        q = subprocess.run([binary, "sessions", "export", "--format", "jsonl",
                            "--session-id", sid, dest], env=home_env(home),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        if q.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest
    legacy = [p for p in glob.glob(os.path.join(home, "sessions", "*.jsonl"))
              if since is None or os.path.getmtime(p) >= since - 1]
    if legacy:
        shutil.copyfile(max(legacy, key=os.path.getmtime), dest)
        return dest
    return None


def _spawn(cmd: list[str], *, env: dict, cwd: str | None, stdout, stderr) -> int:
    """Run hermes in its OWN process group (R1-18): whatever ends this call — an error, a
    SystemExit from terminate_as_exit, KeyboardInterrupt — kills the whole group, so no
    agent tool process outlives the rollout/role run."""
    try:
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=stdout, stderr=stderr,
                             stdin=subprocess.DEVNULL, text=True, start_new_session=True)
    except OSError as e:
        raise RolloutError(f"could not start hermes: {e}") from None
    try:
        return p.wait()
    except BaseException:
        for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                p.wait(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        raise


def _redacted_cmd(cmd: list[str], query_text: str) -> list[str]:
    return ["<query.txt>" if a == query_text else a for a in cmd]


ROLES_DIR = ".hws-roles"


def run(ws: str, prompt: str, *, run_dir: str, home: str | None = None,
        skill_dirs: list[str] | None = None, toolsets: str | None = None,
        model: str | None = None, max_turns: int | None = 15, run_budget: int | None = 300,
        workdir: str | None = None, dry_run: bool = False, hermes: str | None = None,
        copy_skills: bool = False, role: bool = False) -> RunResult:
    """One Hermes rollout. ``skill_dirs`` = exactly the skills the agent must see.

    ``copy_skills`` marks an isolated TASK rollout (rollout.py): skills are copied, not
    linked, and the child's HOME is the rollout home (QA-F2).
    ``role`` (maintainer/proposer, QA-F8): a FRESH home ``<ws>/.hws-roles/r-<id>`` is both
    HERMES_HOME and HOME, holds COPIES of the skills (no symlink out of the ws, T8b), is
    recorded as ``role_home`` in run.json and removed after the session export, so no
    Hermes store (state.db, sessions/, logs/) keeps role transcripts; cwd = the ws."""
    if not role:
        return _run(ws, prompt, run_dir=run_dir, home=home, skill_dirs=skill_dirs, toolsets=toolsets,
                    model=model, max_turns=max_turns, run_budget=run_budget, workdir=workdir,
                    dry_run=dry_run, hermes=hermes, copy_skills=copy_skills)
    roles = os.path.join(ws, ROLES_DIR)
    workdir = workdir or ws
    if dry_run:
        return _run(ws, prompt, run_dir=run_dir, home=os.path.join(roles, "<role-id>"),
                    skill_dirs=skill_dirs, toolsets=toolsets, model=model, max_turns=max_turns,
                    run_budget=run_budget, workdir=workdir, dry_run=True, hermes=hermes)
    if os.path.isdir(roles):  # homes of an aborted earlier run (one role at a time per ws lock)
        for n in os.listdir(roles):
            if n.startswith("r-"):
                shutil.rmtree(os.path.join(roles, n), ignore_errors=True)
    os.makedirs(roles, exist_ok=True)
    with signals.terminate_as_exit():  # R1-18: SIGTERM/SIGHUP still remove the role home
        rhome = tempfile.mkdtemp(prefix="r-", dir=roles)
        try:
            return _run(ws, prompt, run_dir=run_dir, home=rhome, skill_dirs=skill_dirs, toolsets=toolsets,
                        model=model, max_turns=max_turns, run_budget=run_budget, workdir=workdir,
                        dry_run=False, hermes=hermes, copy_skills=True, isolated_home=True)
        finally:
            shutil.rmtree(rhome, ignore_errors=True)
            try:
                os.rmdir(roles)
            except OSError:
                pass


# T11c (QA-F31): while harness.run_role runs a role, the PRIVATE 0700 snapshot dir outside the ws (never
# reachable by the role). Every record this backend writes into the role's run dir gets its sha256 there
# (provenance.json) and the query a copy — the harness trusts a record in the run dir only by that sha.
PROVENANCE_DIR: str | None = None
PROVENANCE_FILE = "provenance.json"
# T11d (QA-F32): PROVENANCE_DIRs a role process was STARTED for (in memory — never reachable by a role);
# the harness fails closed when one of them ends up without a usable provenance file.
PROVENANCE_EXPECTED: set[str] = set()


def _provenance(path: str, data: bytes | None = None, *, keep_copy: bool = False) -> None:
    d = PROVENANCE_DIR
    if not d or not os.path.isdir(d):
        return
    try:
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        name = os.path.basename(path)
        if keep_copy:
            fd = os.open(os.path.join(d, "own-" + name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        rec = read_json(os.path.join(d, PROVENANCE_FILE), {})
        rec = rec if isinstance(rec, dict) else {}
        rec[name] = hashlib.sha256(data).hexdigest()
        atomic_write_json(os.path.join(d, PROVENANCE_FILE), rec)
    except OSError:
        pass


def _run(ws: str, prompt: str, *, run_dir: str, home: str | None = None,
         skill_dirs: list[str] | None = None, toolsets: str | None = None,
         model: str | None = None, max_turns: int | None = 15, run_budget: int | None = 300,
         workdir: str | None = None, dry_run: bool = False, hermes: str | None = None,
         copy_skills: bool = False, isolated_home: bool = False) -> RunResult:
    home = os.path.abspath(home or profile_dir(ws))
    skill_dirs = list(skill_dirs or [])
    toolsets = with_skills(toolsets)
    expected = [skill_name(d) for d in skill_dirs]
    qfile = os.path.join(run_dir, "query.txt")

    if dry_run:  # D3: plan only — nothing is written anywhere
        try:
            info = compat.detect(hermes)
            cmd, cwd = compat.build_chat_argv(info, query_file=qfile, query_text=prompt,
                                              toolsets=toolsets, max_turns=max_turns,
                                              run_budget=run_budget, model=model, workdir=workdir)
            note = None
        except HermesIncompatibleError as e:
            cmd = ["hermes", "chat", "--query-file", qfile, "-Q", "--oneshot", "-t", toolsets]
            cwd, note = workdir, str(e)
        return RunResult(cmd=_redacted_cmd(cmd, prompt), dry_run=True,
                         extra={"run_dir": run_dir, "home": home, "cwd": cwd,
                                "skills_expected": expected, "hermes_note": note})

    info = compat.detect(hermes)
    prof = refresh_profile(ws, home)
    link_skills(home, skill_dirs, copy=copy_skills)
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_text(qfile, prompt)
    if PROVENANCE_DIR:
        PROVENANCE_EXPECTED.add(PROVENANCE_DIR)
    _provenance(qfile, prompt.encode("utf-8"), keep_copy=True)  # before the role starts
    cmd, cwd = compat.build_chat_argv(info, query_file=qfile, query_text=prompt,
                                      toolsets=toolsets, max_turns=max_turns,
                                      run_budget=run_budget, model=model, workdir=workdir)
    out_path = os.path.join(run_dir, "stdout.txt")
    err_path = os.path.join(run_dir, "stderr.txt")
    with rollout_lock():
        t0 = time.time()
        with open(out_path, "w", encoding="utf-8") as fo, open(err_path, "w", encoding="utf-8") as fe:
            code = _spawn(cmd, env=home_env(home, isolated=copy_skills or isolated_home), cwd=cwd,
                          stdout=fo, stderr=fe)
        duration = round(time.time() - t0, 1)
        with open(err_path, encoding="utf-8", errors="replace") as f:
            err_text = f.read()
        with open(out_path, encoding="utf-8", errors="replace") as f:
            out_text = f.read()
        sid = sessions.session_id_from_text(err_text) or sessions.session_id_from_text(out_text)
        sdest = os.path.join(run_dir, "session.jsonl")
        if os.path.lexists(sdest):  # T11c: whatever is there now was written by the run, not the export
            shutil.rmtree(sdest) if os.path.isdir(sdest) and not os.path.islink(sdest) else os.remove(sdest)
        session_file = export_session(info.binary, home, sdest, sid, since=t0)
    for p in (out_path, err_path) + ((session_file,) if session_file else ()):  # the role has exited
        _provenance(p)
    session = sessions.load(session_file) if session_file else {}
    listed = sessions.available_skills(session.get("system_prompt"))
    visible = sorted(n for n in expected if listed and n in listed)
    missing = sorted(set(expected) - set(visible))
    usage = sessions.usage(session)
    record = {
        "cmd": _redacted_cmd(cmd, prompt), "exit_code": code, "duration_s": duration,
        "session_id": sid or session.get("id"),
        "session": "session.jsonl" if session_file else None,
        "hermes_version": info.version, "toolsets": toolsets,
        "skills_expected": expected, "skills_visible": visible,
        "skills_listed": sorted(listed) if listed else [],  # every <available_skills> name (R1-14)
        "skills_index_present": listed is not None,
        "skill_views": sessions.skill_views(session),
        "tool_call_count": sessions.tool_call_count(session) if session else None,
        "usage": usage, "credentials": {"provider": prof["provider"], "env": prof["env_names"]},
    }
    if isolated_home:
        record["role_home"] = home  # the role's HOME: `~` in its session means this dir (T8b)
    atomic_write_json(os.path.join(run_dir, "run.json"), record)
    _provenance(os.path.join(run_dir, "run.json"))

    tail = (err_text.strip().splitlines() or [""])[-1][:300]
    if code == 2 or code == 130 or (code is not None and code < 0):
        raise RolloutError(f"hermes exited {code} (launch/argument failure or interrupt): {tail}")
    if not session_file:
        raise RolloutError(f"hermes exited {code} but no session could be exported "
                           f"(run dir {run_dir}): {tail}")
    if missing:
        raise SkillsInvisibleError(
            f"skills invisible to the agent in {run_dir}: {missing} not in <available_skills> "
            f"of the session system prompt (toolsets={toolsets}); rollout NOT scored")
    if code != 0 and not usage.get("api_call_count") and not session.get("messages"):
        raise RolloutError(f"hermes exited {code} and never ran the model "
                           f"(credentials missing: {prof['missing'] or 'no'}): {tail}")
    return RunResult(cmd=record["cmd"], exit_code=code, duration_s=duration,
                     stdout_path=out_path, session_file=session_file,
                     extra={k: record[k] for k in ("skills_expected", "skills_visible",
                                                   "skill_views", "usage", "hermes_version",
                                                   "session_id", "tool_call_count", "role_home",
                                                   "skills_listed")
                            if k in record})


def load_run_record(run_dir: str) -> dict:
    d = read_json(os.path.join(run_dir, "run.json"), {})
    return d if isinstance(d, dict) else {}


def dumps_cmd(cmd: list[str]) -> str:
    return json.dumps(cmd)
