"""Isolated task rollouts (D19: answer-key leakage; QA-F1..F4).

Host receipt: baseline val rollouts walked up from ``<ws>/bench/tasks/<id>``, found
``<ws>/tasks.json`` and copied the grader's expected output. Every TASK rollout
(run-task, evolve train/val/baseline, gate, eval, compare — not maintainer/proposer)
therefore runs in a fresh directory OUTSIDE the workspace and ``$HWS_HOME``:

    $HWS_ROLLOUT_ROOT (default /tmp/hws-rollouts)/r-<random>/{home,sandbox,run,owner.json}

``home`` = HERMES_HOME (and HOME) with minimal config/.env + COPIES of exactly the
exposed skills; ``sandbox`` = the task files (prompt names only this path; ``--in`` =
sandbox). The sandbox is graded in place, then artifacts go to the workspace run dir
and the rollout dir is ALWAYS deleted (also on SIGTERM/SIGHUP; dirs of dead owners are
swept by the next rollout).

What reaches the workspace (QA-F1 — maintainer/proposer have file+terminal tools there):
TRAIN = everything (session, stdout, sandbox deliverables; D18 feedback needs them);
VAL/TEST = run.json/stderr, ``deliverables.json`` (sha256 + size per file), a REDACTED
session (``sessions.redact``) and digests of stdout/query.txt — a deliverable that scored
1.0 IS the answer, and a held-out prompt is test data (QA-F7).

Escapes (QA-F2): tool-call arguments (paths resolved against the sandbox) AND tool
results (path evidence only; skill_view/skills_list results never — QA-F6) are scanned for
the workspace, ``$HWS_HOME``, ``/home/<user>/ops``, sibling rollouts and — in arguments —
framework names and answer-key file names; any hit → ``sandbox_escape``, score 0.0.
"""


from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time

from . import agents, prompts, scoring, sessions, signals, tasks as tasks_mod
from .errors import HwsError
from .util import atomic_write_json, atomic_write_text, read_json, sha256_file, truncate

DEFAULT_ROOT = "/tmp/hws-rollouts"
ESCAPE_MARKERS = ("hermes-wikiskill", "hermes_wikiskill")
ESCAPE_BASENAMES = ("tasks.json", "test.json")
OPS_RE = re.compile(r"/home/[^/\s\"'`]+/ops(?=$|[/\s\"'`,;)])")
OWNER_FILE = "owner.json"
SKILL_TOOLS = frozenset({"skill_view", "skills_list"})  # read only the rollout's own skills
LEGACY_STALE_S = 6 * 3600  # an r-* dir without owner.json (pre-T5) older than this is stale
_PATH_TOKEN = re.compile(r"[^\s\"'`;|&<>(){}=,]+")
_BASE_RE = re.compile(r"(?:^|[/\s\"'=:*?])(" + "|".join(map(re.escape, ESCAPE_BASENAMES))
                      + r")(?:$|[\s\"',;)\\])")  # "\\": JSON-escaped tool results ("…tasks.json\\n")


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def hws_home() -> str:
    return os.path.abspath(os.path.expanduser(os.environ.get("HWS_HOME") or "~/ops/hermes-wikiskill"))


def rollout_root(ws: str) -> str:
    root = os.path.abspath(os.path.expanduser(os.environ.get("HWS_ROLLOUT_ROOT") or DEFAULT_ROOT))
    for forbidden in (ws, hws_home()):
        if _inside(root, forbidden) or _inside(forbidden, root):
            raise HwsError(f"HWS_ROLLOUT_ROOT {root} must be outside {forbidden} (D19 isolation)")
    return root


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale(root: str, *, now: float | None = None) -> list[str]:
    """QA-F4: remove ``r-*`` rollout dirs whose owner process is gone (a SIGKILLed run
    leaves its home/.env behind). Dirs of live owners are never touched."""
    removed = []
    if not os.path.isdir(root):
        return removed
    now = time.time() if now is None else now
    for n in sorted(os.listdir(root)):
        d = os.path.join(root, n)
        if not n.startswith("r-") or os.path.islink(d) or not os.path.isdir(d):
            continue
        owner = read_json(os.path.join(d, OWNER_FILE), None)
        if isinstance(owner, dict):
            stale = not _pid_alive(owner.get("pid"))
        else:
            try:
                stale = now - os.lstat(d).st_mtime > LEGACY_STALE_S
            except OSError:
                continue
        if stale:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed


def _resolved_paths(text: str, cwd: str) -> list[str]:
    """Path-like tokens of a tool argument, ``..``/relative ones resolved against ``cwd``."""
    out = []
    for tok in _PATH_TOKEN.findall(text):
        if "://" in tok or not ("/" in tok or tok == ".."):
            continue
        if tok.startswith("~"):
            continue  # HOME is the rollout home (QA-F2)
        out.append(os.path.normpath(tok if os.path.isabs(tok) else os.path.join(cwd, tok)))
    return out


def detect_escapes(session: dict, ws: str, *, sandbox: str | None = None,
                   rollout_dir: str | None = None, excerpts: bool = True) -> list[str]:
    """Tool calls that reach for the workspace, ``$HWS_HOME``, an ``~/ops`` tree, the
    framework, an answer-key file or another rollout (QA-F2).

    Arguments: path evidence (literal or resolved against the sandbox) + marker words
    (framework names, tasks.json/test.json). RESULTS: path evidence only, and never for
    ``skill_view``/``skills_list`` — exposed skill text may name anything (QA-F6).

    ``excerpts=False`` (VAL/TEST) replaces the offending text with its digest so the
    escape record itself never carries an answer into the workspace."""
    ws_paths = {os.path.abspath(ws), os.path.realpath(ws)}
    home_paths = {hws_home(), os.path.realpath(hws_home())}
    ops_home = os.path.join(os.path.expanduser("~"), "ops")
    own = os.path.realpath(rollout_dir) if rollout_dir else None
    root = os.path.dirname(own) if own else None
    roots = sorted({root, os.path.dirname(os.path.abspath(rollout_dir))}) if own else []
    sib_re = (re.compile("(?:" + "|".join(map(re.escape, roots)) + r")/(r-[^/\s\"'`]+)") if roots else None)

    own_forms = sorted({own, os.path.abspath(rollout_dir)} if own else set(), key=len, reverse=True)

    def reasons(text: str, *, args: bool, markers: bool) -> set[str]:
        why = set()
        raw = text
        for o in own_forms:  # the rollout's own paths never count (whatever HWS_ROLLOUT_ROOT is)
            text = text.replace(o, "<rollout>")
        if any(w in text for w in ws_paths):
            why.add("workspace path")
        if any(h in text for h in home_paths):
            why.add("HWS_HOME")
        if OPS_RE.search(text) or ops_home + "/" in text or text.rstrip().endswith(ops_home):
            why.add("~/ops")
        if markers:  # bare words only count in what the agent TYPED, never in what it read (QA-F6)
            why.update(m for m in ESCAPE_MARKERS if m in text)
            why.update(m.group(1) for m in _BASE_RE.finditer(text))
        if sib_re:
            why.update("other rollout" for m in sib_re.finditer(text)
                       if m.group(1) != os.path.basename(own))
        if args and sandbox:
            for p in _resolved_paths(raw, sandbox):
                if any(_inside(p, w) for w in ws_paths):
                    why.add("workspace path")
                if any(_inside(p, h) for h in home_paths):
                    why.add("HWS_HOME")
                if root and _inside(p, root) and not _inside(p, own):
                    why.add("other rollout")
        return why

    hits = []
    items = [(n, "args", a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
             for n, a in sessions.tool_calls(session)]
    items += [(n, "result", t) for n, t in sessions.tool_results(session)]
    for name, kind, text in items:
        if kind == "result" and name in SKILL_TOOLS:
            continue  # the rollout's own exposed skills: allowed reading, whatever they mention
        why = reasons(text, args=kind == "args", markers=kind == "args" and name not in SKILL_TOOLS)
        if why:
            shown = truncate(text, 200) if excerpts else sessions.digest(text)
            where = "" if kind == "args" else " result"
            hits.append(f"{name}{where} [{', '.join(sorted(why))}]: {shown}")
    return hits


_cleanup_on_signals = signals.terminate_as_exit  # moved to signals.py (shared with role runs, R1-18)


def _deliverables(sandbox: str) -> dict:
    out = {}
    for dirpath, dirnames, files in os.walk(sandbox):
        dirnames.sort()
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, sandbox)
            if os.path.islink(p):
                out[rel] = {"symlink": True}
            else:
                out[rel] = {"sha256": sha256_file(p), "size": os.path.getsize(p)}
    return out


def redact_run_dir(run_dir: str, *, query: bool = True) -> None:
    """Replace the session, stdout (the agent's final answer) and — ``query`` — the task
    prompt (QA-F7: a held-out TEST prompt is test data too) by digests, in place."""
    sp = os.path.join(run_dir, "session.jsonl")
    if os.path.isfile(sp):
        lines = []
        with open(sp, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    obj = {"unparsed": line}
                lines.append(json.dumps(sessions.redact(obj) if isinstance(obj, dict)
                                        else sessions.digest(obj), ensure_ascii=False))
        atomic_write_text(sp, "\n".join(lines) + ("\n" if lines else ""))
    op = os.path.join(run_dir, "stdout.txt")
    if os.path.isfile(op):
        with open(op, encoding="utf-8", errors="replace") as f:
            text = f.read()
        atomic_write_text(op, f"<redacted: VAL/TEST stdout {sessions.digest(text)}>\n")
    qp = os.path.join(run_dir, "query.txt")
    if query and os.path.isfile(qp):
        with open(qp, encoding="utf-8", errors="replace") as f:
            text = f.read()
        atomic_write_text(qp, f"<redacted: VAL/TEST prompt {sessions.digest(text)}>\n")


def is_sealed_split(task: dict) -> bool:
    """Everything but TRAIN keeps no answer-bearing artifact in the workspace (QA-F1)."""
    return task.get("split") != "train"


def run_task_rollout(ws: str, task: dict, *, run_dir: str, skill_dirs: list[str],
                     model: str | None = None, runner=agents.run_agent, dry_run: bool = False,
                     runner_kwargs: dict | None = None, tag: str | None = None) -> dict:
    """One isolated task rollout. Returns {score, feedback?, escapes, result}."""
    kw = dict(runner_kwargs or {})
    root = rollout_root(ws)
    tag = tag or os.path.relpath(run_dir, ws)
    if dry_run:
        rdir = os.path.join(root, "<rollout-id>")
        sandbox = os.path.join(rdir, "sandbox")
        res = runner(ws, prompts.inference_prompt(task, sandbox=sandbox), tag=tag,
                     run_dir=os.path.join(rdir, "run"), home=os.path.join(rdir, "home"),
                     skill_dirs=skill_dirs, workdir=sandbox, model=model, dry_run=True,
                     copy_skills=True, **kw)
        return {"score": None, "escapes": [], "result": res}
    sealed = is_sealed_split(task)
    os.makedirs(root, mode=0o700, exist_ok=True)
    sweep_stale(root)
    with _cleanup_on_signals():
        rdir = tempfile.mkdtemp(prefix="r-", dir=root)
        atomic_write_json(os.path.join(rdir, OWNER_FILE), {"pid": os.getpid()})
        tmp_run = os.path.join(rdir, "run")
        sandbox = os.path.join(rdir, "sandbox")
        copied = False

        def save_artifacts() -> None:
            if os.path.exists(run_dir):
                shutil.rmtree(run_dir)
            if os.path.isdir(tmp_run):
                shutil.copytree(tmp_run, run_dir, symlinks=True)
            else:
                os.makedirs(run_dir, exist_ok=True)
            if sealed:
                redact_run_dir(run_dir)
                if os.path.isdir(sandbox):
                    atomic_write_json(os.path.join(run_dir, "deliverables.json"), _deliverables(sandbox))
            elif os.path.isdir(sandbox):
                shutil.copytree(sandbox, os.path.join(run_dir, "sandbox"), symlinks=True)

        try:
            tasks_mod.materialize_at(sandbox, task, force=True)
            try:
                res = runner(ws, prompts.inference_prompt(task, sandbox=sandbox), tag=tag, run_dir=tmp_run,
                             home=os.path.join(rdir, "home"), skill_dirs=skill_dirs, workdir=sandbox,
                             model=model, dry_run=False, copy_skills=True, **kw)
            except HwsError as e:  # R1-12: name the ws run dir that keeps the evidence, not tmp_run
                if tmp_run in str(e):
                    raise type(e)(str(e).replace(tmp_run, run_dir)) from None
                raise
            score = scoring.grade(task, sandbox)
            out: dict = {"score": score}
            if task.get("split") == "train":  # D18: TRAIN only
                out["feedback"] = scoring.feedback(task, sandbox)
            session = sessions.load(res["session_file"]) if res.get("session_file") else {}
            escapes = detect_escapes(session, ws, sandbox=sandbox, rollout_dir=rdir, excerpts=not sealed)
            if escapes:
                out["score"] = 0.0
            out["escapes"] = escapes
            save_artifacts()
            copied = True
            rec = read_json(os.path.join(run_dir, "run.json"), {})
            if isinstance(rec, dict):
                rec.update({"score": out["score"], "sandbox_escape": escapes, "isolated": True,
                            "redacted": sealed})
                atomic_write_json(os.path.join(run_dir, "run.json"), rec)
            session_copy = os.path.join(run_dir, "session.jsonl")
            out["result"] = {**res, "session_file": session_copy if os.path.exists(session_copy) else None,
                             "stdout_path": os.path.join(run_dir, "stdout.txt"), "escapes": escapes}
            return out
        finally:
            if not copied:
                try:
                    save_artifacts()  # keep evidence of a failed rollout (never the answer key)
                except OSError:
                    pass
            shutil.rmtree(rdir, ignore_errors=True)
            try:
                os.rmdir(root)  # leave no empty /tmp/hws-rollouts behind
            except OSError:
                pass
