"""Task graders (upstream schemas) + TRAIN-only grader feedback (D18) + sealed graders (D19).

Sealed: ``exact`` / ``json_field`` / ``code_stdout`` may carry ``expected_sha256`` (sha256 hex of
the UTF-8 ``normalize(expected)``) instead of ``expected`` — val/test answer keys never sit in
plaintext where an agent could read them.

  {"type": "exact",       "file": "output.txt", "expected": "..."}  normalized equality
  {"type": "contains",    "file": "output.txt", "needle": "..."}    substring
  {"type": "json_field",  "file": "out.json", "path": [...], "expected": "..."}
  {"type": "code_stdout", "script": "solve.py", "expected": "..."}  run script; exit 0 AND stdout match (D12)
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys

from .errors import UnsafePathError
from .util import safe_join, truncate

FEEDBACK_LIMIT = 2048
_PART = 900


def normalize(s: object) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def seal_digest(expected: object) -> str:
    return hashlib.sha256(normalize(expected).encode("utf-8")).hexdigest()


def matches(got: object, g: dict) -> bool:
    """Normalized equality against ``expected`` or the sealed ``expected_sha256``."""
    if "expected_sha256" in g:
        return seal_digest(got) == str(g["expected_sha256"]).lower()
    return normalize(got) == normalize(g["expected"])


def is_sealed(task: dict) -> bool:
    return "expected_sha256" in (task.get("grader") or {})


def read_sandbox_file(sandbox: str, rel: str) -> str:
    with open(safe_join(sandbox, rel), encoding="utf-8", errors="replace") as f:
        return f.read()


def _run_script(sandbox: str, script: str) -> subprocess.CompletedProcess:
    safe_join(sandbox, script)
    return subprocess.run([sys.executable, script], cwd=sandbox, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)


def grade(task: dict, sandbox: str) -> float:
    g = task["grader"]
    t = g["type"]
    try:
        if t == "exact":
            return 1.0 if matches(read_sandbox_file(sandbox, g["file"]), g) else 0.0
        if t == "contains":
            return 1.0 if g["needle"] in read_sandbox_file(sandbox, g["file"]) else 0.0
        if t == "json_field":
            val = json.loads(read_sandbox_file(sandbox, g["file"]))
            for key in g["path"]:
                val = val[key]
            return 1.0 if matches(val, g) else 0.0
        if t == "code_stdout":
            p = _run_script(sandbox, g.get("script", "solve.py"))
            if p.returncode != 0:  # D12: a crashing script never passes
                return 0.0
            return 1.0 if matches(p.stdout, g) else 0.0
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError,
            UnsafePathError, subprocess.TimeoutExpired):
        return 0.0
    raise ValueError(f"unknown grader type: {t!r}")


def feedback(task: dict, sandbox: str) -> dict:
    """What the grader expected vs what the agent produced (≤ 2 KB). TRAIN split only."""
    g = task["grader"]
    t = g["type"]
    fb: dict = {"grader": t}
    try:
        if t in ("exact", "contains", "json_field"):
            fb["file"] = g["file"]
            try:
                got = read_sandbox_file(sandbox, g["file"])
            except (OSError, UnsafePathError):
                got = None
            fb["got"] = "<missing file>" if got is None else truncate(got, _PART)
            fb["expected"] = ("<sealed>" if "expected_sha256" in g
                              else truncate(g.get("expected", g.get("needle")), _PART))
            if t == "json_field":
                fb["path"] = g.get("path")
        elif t == "code_stdout":
            fb["script"] = g.get("script", "solve.py")
            fb["expected"] = "<sealed>" if "expected_sha256" in g else truncate(g.get("expected"), _PART)
            try:
                p = _run_script(sandbox, fb["script"])
                fb["got"] = truncate(p.stdout, _PART)
                fb["exit_code"] = p.returncode
            except (OSError, UnsafePathError, subprocess.TimeoutExpired) as e:
                fb["got"] = f"<error: {e.__class__.__name__}>"
    except KeyError:
        pass
    raw = json.dumps(fb)
    while len(raw) > FEEDBACK_LIMIT and ("got" in fb or "expected" in fb):
        for k in ("got", "expected"):
            if k in fb:
                fb[k] = truncate(fb[k], max(40, len(fb[k]) // 2))
        raw = json.dumps(fb)
    return fb
