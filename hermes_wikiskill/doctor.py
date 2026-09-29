"""`doctor`: install / Hermes / profile health (INTERFACE §3.1). Exit 0 iff no `fail`.

Fixed check ids: python hermes-binary hermes-version hermes-flags profile profile-toolsets
profile-credentials operator-skill framework-skills skills-visibility workspace-locks disk
ram (+ live-run with --live, + pass-env when HWS_PASS_ENV is set). ``detail`` never
contains a secret value.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time

from . import __version__, compat, credentials, mini_yaml, profiles, resources, sessions
from .errors import HwsError
from .locking import holder_pid, is_locked, lock_path
from .skillfiles import frontmatter
from .util import atomic_write_text, stamp

DISK_OK_MB, DISK_FAIL_MB = 1024, 300
RAM_FLOOR_MB = 256  # documented hard floor: one Hermes rollout (+ grader) needs ~250 MB
MESSAGING = re.compile(r"(TELEGRAM|DISCORD|SLACK|WHATSAPP|SIGNAL|MATRIX|BOT_TOKEN|WEBHOOK|SMTP|IMAP)", re.I)


def _check(cid: str, status: str, detail: str, **extra) -> dict:
    return {"id": cid, "status": status, "detail": detail, **extra}


def _hws_home() -> str:
    return profiles.hws_home()


def _read_config(home: str) -> dict:
    try:
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as f:
            return mini_yaml.loads(f.read())
    except OSError:
        return {}


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if x is not None]
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def check_toolsets(cfg: dict) -> tuple[str, str]:
    agent = cfg.get("agent")
    if agent is not None and not isinstance(agent, dict):  # R1-15: e.g. an inline map `agent: {…}`
        if "skills" in str(agent):
            return "fail", ("config `agent` is not a block mapping and mentions `skills` (inline map?); "
                            "it may disable the skills toolset (D17) — rewrite it as a block mapping")
        return "warn", (f"config `agent` is not a block mapping ({type(agent).__name__}); disabled_toolsets "
                        "cannot be verified — rewrite it as a block mapping")
    agent = agent or {}
    disabled = _as_list(agent.get("disabled_toolsets"))
    if "skills" in disabled:
        return "fail", "config agent.disabled_toolsets removes `skills` (D17: skills would be invisible)"
    plat = cfg.get("platform_toolsets") if isinstance(cfg.get("platform_toolsets"), dict) else {}
    cli = _as_list(plat.get("cli")) if "cli" in plat else None
    if cli is not None and not ({"skills", "all", "*"} & set(cli)):
        return "fail", f"platform_toolsets.cli={cli} lacks `skills`"
    return "ok", f"skills toolset enabled (disabled_toolsets={disabled or []})"


def check_credentials(home: str) -> tuple[str, str]:
    problems = []
    cfg = _read_config(home)
    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if "api_key" in model or "providers" in cfg:
        problems.append("config.yaml holds an inline api_key/providers section")
    envp = os.path.join(home, ".env")
    env = credentials.read_dotenv(envp)
    keys, base = credentials.env_names(model.get("provider"))
    allowed = set(keys) | ({base} if base else set())
    extra = sorted(set(env) - allowed)
    if extra:
        problems.append(f".env has non-provider names {extra}")
    if [k for k in env if MESSAGING.search(k)]:
        problems.append(".env holds messaging tokens")
    if os.path.exists(envp) and stat.S_IMODE(os.stat(envp).st_mode) != 0o600:
        problems.append(".env mode is not 0600")
    if not any(env.get(k) for k in keys):
        problems.append(f"missing provider key {keys[0]}")
    problems += credentials.auth_json_issues(home, model.get("provider")) or []
    if problems:
        return "fail", "; ".join(problems)
    return "ok", f"only {sorted(env)} (0600), key-less config, auth.json absent or fingerprint-only"


def _operator_skill(skills: str) -> tuple[str, str]:
    p = os.path.join(skills, resources.OPERATOR_SKILL, "SKILL.md")
    try:
        with open(p, encoding="utf-8") as f:
            fm = frontmatter(f.read())
    except OSError:
        return "fail", f"missing {p} (run `hermes-wikiskill setup-profile`)"
    if fm.get("name") != resources.OPERATOR_SKILL or not fm.get("description"):
        return "fail", f"{p}: frontmatter needs name: {resources.OPERATOR_SKILL} + description"
    return "ok", p


def _mem_available_mb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def _served_profiles() -> list | None:
    """``served_profiles`` recorded by the live default gateway (read-only, R1-qa fact 34)."""
    import json
    try:
        with open(os.path.join(profiles.hermes_root(), "gateway_state.json"), encoding="utf-8") as f:
            served = json.load(f).get("served_profiles")
    except (OSError, ValueError, AttributeError):
        return None
    return served if isinstance(served, list) else None


def live_probe(home: str, *, hermes: str | None = None, out_dir: str | None = None) -> dict:
    """ONE real rollout in the profile home; proves the operator skill is in <available_skills>."""
    info = compat.detect(hermes)
    run_dir = out_dir or os.path.join(_hws_home(), "doctor", stamp())
    os.makedirs(run_dir, exist_ok=True)
    q = os.path.join(run_dir, "query.txt")
    text = "Reply with the single word OK. Do not use any tools."
    atomic_write_text(q, text)
    cmd, cwd = compat.build_chat_argv(info, query_file=q, query_text=text, toolsets="skills",
                                      max_turns=2, run_budget=120, model=None, workdir=None)
    env = {**os.environ, "HERMES_HOME": os.path.abspath(home)}
    t0 = time.time()
    p = subprocess.run(cmd, env=env, cwd=cwd or run_dir, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=600)
    from .backends.hermes import export_session
    sid = sessions.session_id_from_text(p.stderr) or sessions.session_id_from_text(p.stdout)
    sf = export_session(info.binary, home, os.path.join(run_dir, "session.jsonl"), sid, since=t0)
    s = sessions.load(sf) if sf else {}
    listed = sessions.available_skills(s.get("system_prompt")) or set()
    return {"exit_code": p.returncode, "session": sf, "listed": sorted(listed),
            "visible": resources.OPERATOR_SKILL in listed, "usage": sessions.usage(s)}


def run_checks(profile: str | None = None, *, live: bool = False, hermes: str | None = None,
               ws_root: str | None = None) -> dict:
    profile = profile or profiles.DEFAULT_PROFILE
    checks = []
    v = sys.version_info
    checks.append(_check("python", "ok" if v >= (3, 10) else "fail",
                         f"{platform.python_version()} ({sys.executable})"))
    info = None
    try:
        binary = compat.resolve_binary(hermes)
        checks.append(_check("hermes-binary", "ok", binary))
        ver = compat.hermes_version(binary)
        checks.append(_check("hermes-version", "ok" if ver else "warn",
                             ver or "unknown (package metadata not found; flags are still probed)"))
        try:
            info = compat.detect(binary)
            compat.require_usable(info)
            checks.append(_check("hermes-flags", "ok" if info.modern else "warn",
                                 "modern argv (--query-file --oneshot)" if info.modern
                                 else "legacy argv fallback (-q, cwd instead of --in)"))
        except HwsError as e:
            checks.append(_check("hermes-flags", "fail", str(e)))
    except HwsError as e:
        checks.append(_check("hermes-binary", "fail", str(e)))
        checks.append(_check("hermes-version", "fail", "no hermes binary"))
        checks.append(_check("hermes-flags", "fail", "no hermes binary"))

    home = profiles.profile_dir(profile)
    # foreign = default or any existing profile without our marker: only the operator skill is ours (R1-06)
    owner = profile == "default" or (profiles.is_live(profile) and not profiles.is_ours(profile))
    if owner:
        checks.append(_check("profile", "ok" if os.path.isdir(home) else "fail",
                             f"{home} (foreign profile, not created by hermes-wikiskill: only the operator "
                             f"skill is managed)"))
    elif not profiles.is_live(profile):
        checks.append(_check("profile", "fail", f"{home} missing (run `hermes-wikiskill setup-profile`)"))
    else:
        served = _served_profiles()
        if served is not None and profile in served:  # H7/Q11: a multiplexing gateway serves it
            checks.append(_check("profile", "warn", f"{home} is served by the live gateway "
                                                    f"(served_profiles={served}); it holds no messaging tokens"))
        else:
            checks.append(_check("profile", "ok", f"{home} (not served by a gateway: "
                                                  f"served_profiles={served if served is not None else 'n/a'})"))
    cfg = _read_config(home)
    st, detail = check_toolsets(cfg)
    checks.append(_check("profile-toolsets", st, detail))
    if owner:
        checks.append(_check("profile-credentials", "ok", "foreign profile: credentials not managed/inspected"))
    else:
        st, detail = check_credentials(home)
        checks.append(_check("profile-credentials", st, detail))
    st, detail = _operator_skill(os.path.join(home, "skills"))
    checks.append(_check("operator-skill", st, detail))
    try:
        found = resources.require_framework_skills()
        checks.append(_check("framework-skills", "ok", ", ".join(n for n, _ in found)))
    except FileNotFoundError as e:
        checks.append(_check("framework-skills", "fail", str(e)))

    vis_ok = (checks[-2]["status"] == "ok" and check_toolsets(cfg)[0] == "ok")
    checks.append(_check("skills-visibility", "ok" if vis_ok else "fail",
                         f"Hermes indexes {os.path.join(home, 'skills')} (HERMES_HOME/skills) with the "
                         f"skills toolset on" if vis_ok else "operator skill missing or skills toolset disabled"))

    root = os.path.expanduser(ws_root or os.path.join(_hws_home(), "workspaces"))
    busy = []
    if os.path.isdir(root):
        for n in sorted(os.listdir(root)):
            w = os.path.join(root, n)
            if os.path.exists(lock_path(w)) and is_locked(w):
                busy.append(f"{n} (pid {holder_pid(lock_path(w)) or '?'})")
    checks.append(_check("workspace-locks", "ok",
                         f"busy: {', '.join(busy)}" if busy else f"no workspace busy under {root}"))

    probe = _hws_home()
    while not os.path.exists(probe):
        probe = os.path.dirname(probe)
    free = shutil.disk_usage(probe).free / 2**20
    checks.append(_check("disk", "ok" if free >= DISK_OK_MB else "warn" if free >= DISK_FAIL_MB else "fail",
                         f"{free:.0f} MB free at {probe}"))
    mem = _mem_available_mb()
    checks.append(_check("ram", "ok" if mem is None or mem >= RAM_FLOOR_MB else "warn",
                         f"{mem:.0f} MB available (floor {RAM_FLOOR_MB} MB)" if mem is not None
                         else "not measurable on this platform (no /proc/meminfo)"))

    if os.environ.get("HWS_PASS_ENV"):  # R1-19: a test-double switch; names only, never values
        checks.append(_check("pass-env", "warn",
                             f"HWS_PASS_ENV is set ({len(_as_list(os.environ['HWS_PASS_ENV']))} name(s)): extra "
                             "caller env reaches hermes children when HWS_TESTING=1; unset it outside tests"))
    if live:
        try:
            r = live_probe(home, hermes=hermes)
            checks.append(_check("live-run", "ok" if r["visible"] and r["exit_code"] == 0 else "fail",
                                 f"exit {r['exit_code']}, operator skill "
                                 f"{'listed' if r['visible'] else 'NOT listed'} in <available_skills> "
                                 f"(session {r['session']}, cost {r['usage'].get('cost_usd')})"))
        except (HwsError, OSError, subprocess.TimeoutExpired) as e:
            checks.append(_check("live-run", "fail", str(e)))
    return {"tool": "hermes-wikiskill", "version": __version__, "profile": profile,
            "ok": not any(c["status"] == "fail" for c in checks), "checks": checks}
