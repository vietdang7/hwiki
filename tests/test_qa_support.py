"""qa helpers for the adversarial test_qa_* modules (no tests here). Owner: qa.

- strict_snapshot: every path's type, mode, size, sha256, mtime_ns and symlink target (core's tree_hash ignores mode/mtime,
  so a dry-run that rewrites identical bytes or chmods a file would still pass there).
- hold_lock: a separate process holding a workspace flock, like a concurrent hermes-wikiskill run.
- qa_hermes: a wrapper around core's fake Hermes that (a) can drop one skill from the exported system prompt
  (QA_DROP_SKILL) and (b) records, at chat time, what the rollout's HERMES_HOME holds and which environment variable
  NAMES the agent process inherits (names/modes, never values).
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap

from conftest import FAKE_HERMES


def strict_snapshot(root):
    snap = {}
    if not os.path.lexists(root):
        return snap
    for dirpath, dirnames, filenames in os.walk(root):
        for n in sorted(dirnames + filenames):
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                snap[rel] = ("l", os.readlink(p), st.st_mtime_ns)
            elif stat.S_ISDIR(st.st_mode):
                snap[rel] = ("d", stat.S_IMODE(st.st_mode), st.st_mtime_ns)
            else:
                with open(p, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                snap[rel] = ("f", stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, digest)
    st = os.lstat(root)
    snap["."] = ("d", stat.S_IMODE(st.st_mode), st.st_mtime_ns)
    return snap


def snapshot_diff(a, b):
    out = {}
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out[k] = (a.get(k), b.get(k))
    return out


def hold_lock(path, seconds=60):
    code = textwrap.dedent(f"""
        import fcntl, os, time
        fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
        print("locked", flush=True)
        time.sleep({seconds})
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


WRAPPER = r'''#!{python}
import json, os, re, stat, subprocess, sys
FAKE = {fake!r}
argv = sys.argv[1:]
log = os.environ.get("QA_HERMES_LOG")
if argv[:1] == ["chat"] and "--help" not in argv and log:
    home = os.environ.get("HERMES_HOME", "")
    rec = {{"home": home, "files": {{}}, "env_names": sorted(os.environ), "home_env": os.environ.get("HOME"), "cwd": os.getcwd()}}
    for n in (".env", "auth.json", "config.yaml"):
        p = os.path.join(home, n)
        if os.path.exists(p):
            text = open(p, encoding="utf-8", errors="replace").read()
            rec["files"][n] = {{"mode": "%o" % stat.S_IMODE(os.stat(p).st_mode),
                               "env_names": sorted(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.M)) if n == ".env" else None,
                               "api_key_lines": len(re.findall(r"^\s*api_key\s*:\s*\S", text, re.M)) if n == "config.yaml" else None}}
    with open(log, "a") as f:
        f.write(json.dumps(rec) + "\n")
rc = subprocess.run([FAKE] + argv).returncode
drop = os.environ.get("QA_DROP_SKILL")
if drop and argv[:2] == ["sessions", "export"] and rc == 0:
    for a in argv[2:]:
        if a.endswith(".jsonl") and os.path.isfile(a):
            rows = [json.loads(l) for l in open(a) if l.strip()]
            for r in rows:
                sp = r.get("system_prompt") or ""
                r["system_prompt"] = "\n".join(l for l in sp.split("\n")
                                               if not re.match(r"^\s+(- )?%s(:|$)" % re.escape(drop), l))
            with open(a, "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
sys.exit(rc)
'''


def qa_hermes(tmp_path, monkeypatch, drop=None):
    """Install the wrapper as HERMES_BIN and first on PATH; returns the chat-time home log path."""
    d = tmp_path / "qa-hermes-bin"
    d.mkdir(exist_ok=True)
    w = d / "hermes"
    w.write_text(WRAPPER.format(python=sys.executable, fake=FAKE_HERMES))
    w.chmod(0o755)
    log = tmp_path / "qa-hermes-homes.jsonl"
    monkeypatch.setenv("HERMES_BIN", str(w))
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("QA_HERMES_LOG", str(log))
    if drop:
        monkeypatch.setenv("QA_DROP_SKILL", drop)
    else:
        monkeypatch.delenv("QA_DROP_SKILL", raising=False)
    from hermes_wikiskill import compat
    compat._CACHE.clear()
    return log


def chat_homes(log):
    if not os.path.exists(log):
        return []
    return [json.loads(line) for line in open(log) if line.strip()]
