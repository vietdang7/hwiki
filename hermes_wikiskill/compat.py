"""Hermes feature detection (D6, H1).

- The version comes from the package metadata of the Hermes install
  (``<venv python> -I -B -c importlib.metadata…``), never from ``hermes --version``
  (which writes ``~/.hermes/.update_check`` and calls GitHub, qa-H08).
- Flags come from ``hermes chat --help`` run under a scratch HERMES_HOME
  (``/tmp/hws-probe-*``, removed afterwards), cached per binary path + mtime.
- The argv is built from the flags that are actually present: modern
  (``--query-file --oneshot -Q --run-budget --in``) or the legacy fallback
  (``-q <text>``, cwd instead of ``--in``) — an unknown flag is never passed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .errors import HermesIncompatibleError

_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*|-[A-Za-z])\b")
_CACHE: dict[tuple[str, float], "HermesInfo"] = {}
HERMES_DISTS = ("hermes-agent", "hermes_agent", "hermes")


@dataclass
class HermesInfo:
    binary: str
    version: str | None
    flags: set[str] = field(default_factory=set)

    @property
    def query_file(self) -> bool:
        return "--query-file" in self.flags

    @property
    def modern(self) -> bool:
        return {"--query-file", "--oneshot"} <= self.flags

    def as_dict(self) -> dict:
        return {"binary": self.binary, "version": self.version, "modern": self.modern,
                "flags": sorted(self.flags)}


def resolve_binary(hermes: str | None = None) -> str:
    cand = hermes or os.environ.get("HERMES_BIN") or "hermes"
    path = cand if os.sep in cand else shutil.which(cand)
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise HermesIncompatibleError(f"hermes binary not found or not executable: {cand!r} "
                                      f"(set --hermes or HERMES_BIN)")
    return os.path.abspath(path)


def scratch_env(home: str) -> dict:
    env = dict(os.environ)
    env["HERMES_HOME"] = home
    env.pop("HERMES_KANBAN_TASK", None)
    return env


_SHELLS = ("sh", "bash", "dash", "zsh", "ksh")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=[\"']?([^\"'\s;]*)", re.M)
_EXEC = re.compile(r"^\s*exec\s+[\"']?([^\"'\s;]+)", re.M)
FALLBACK_VENVS = ("~/.hermes/hermes-agent/venv", "~/.hermes/hermes-agent/.venv")


def _shebang(binary: str) -> list[str]:
    try:
        with open(os.path.realpath(binary), "rb") as f:
            first = f.readline(512).decode("utf-8", "replace").strip()
    except OSError:
        return []
    return first[2:].split() if first.startswith("#!") else []


def _interpreter(binary: str) -> str | None:
    parts = _shebang(binary)
    if not parts:
        return None
    if os.path.basename(parts[0]) == "env":
        parts = parts[1:]
        while parts and parts[0].startswith("-"):
            parts = parts[1:]
        return shutil.which(parts[0]) if parts else None
    return parts[0]


def _expand(value: str, variables: dict[str, str]) -> str:
    def sub(m):
        name = m.group(1) or m.group(2)
        return variables.get(name, os.environ.get(name, m.group(0)))
    return os.path.expanduser(re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", sub, value))


def _python_candidates(binary: str, depth: int = 0) -> list[str]:
    """Pythons that may own the Hermes install behind ``binary`` (console script or shell wrapper)."""
    out: list[str] = []
    py = _interpreter(binary)
    if py and "python" in os.path.basename(py):
        out.append(py)
    elif py and os.path.basename(py) in _SHELLS and depth < 3:
        try:
            with open(os.path.realpath(binary), encoding="utf-8", errors="replace") as f:
                text = f.read(65536)
        except OSError:
            text = ""
        variables: dict[str, str] = {}
        for name, value in _ASSIGN.findall(text):
            variables[name] = _expand(value, variables)
        venv = variables.get("HERMES_VENV")
        if venv:
            out.append(os.path.join(venv, "bin", "python"))
        for target in _EXEC.findall(text):
            target = _expand(target, variables)
            if os.path.isfile(target) and os.path.realpath(target) != os.path.realpath(binary):
                out += _python_candidates(target, depth + 1)
    if depth == 0:
        out += [os.path.join(os.path.expanduser(v), "bin", "python") for v in FALLBACK_VENVS]
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def hermes_version(binary: str) -> str | None:
    """Version of the Hermes install from its package metadata (no `hermes` call)."""
    code = ("import importlib.metadata as m\n"
            f"for d in {HERMES_DISTS!r}:\n"
            "    try:\n        print(m.version(d)); break\n"
            "    except m.PackageNotFoundError:\n        pass\n")
    for py in _python_candidates(binary):
        if not (os.path.isfile(py) and os.access(py, os.X_OK)):
            continue
        try:
            p = subprocess.run([py, "-I", "-B", "-c", code], capture_output=True, text=True,
                               timeout=30, env={"PATH": os.environ.get("PATH", "")})
        except (OSError, subprocess.TimeoutExpired):
            continue
        v = p.stdout.strip().splitlines()
        if p.returncode == 0 and v:
            return v[0]
    return None


def probe_flags(binary: str) -> set[str]:
    home = tempfile.mkdtemp(prefix="hws-probe-", dir="/tmp")
    try:
        p = subprocess.run([binary, "chat", "--help"], capture_output=True, text=True,
                           timeout=60, env=scratch_env(home), cwd=home)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HermesIncompatibleError(f"`hermes chat --help` failed: {e}") from None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if p.returncode != 0:
        raise HermesIncompatibleError(
            f"`hermes chat --help` exited {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}")
    return set(_FLAG.findall(p.stdout + "\n" + p.stderr))


def detect(hermes: str | None = None) -> HermesInfo:
    binary = resolve_binary(hermes)
    key = (binary, os.stat(binary).st_mtime)
    if key not in _CACHE:
        _CACHE[key] = HermesInfo(binary=binary, version=hermes_version(binary),
                                 flags=probe_flags(binary))
    return _CACHE[key]


def require_usable(info: HermesInfo) -> None:
    missing = []
    if not ({"--query-file"} & info.flags or {"-q", "--query"} & info.flags):
        missing.append("--query-file or -q/--query")
    if not {"-t", "--toolsets"} & info.flags:
        missing.append("-t/--toolsets (needed to enable the skills toolset)")
    if missing:
        raise HermesIncompatibleError(
            f"Hermes at {info.binary} (version {info.version or 'unknown'}) lacks "
            f"required chat flags: {', '.join(missing)}")


def build_chat_argv(info: HermesInfo, *, query_file: str, query_text: str, toolsets: str,
                    max_turns: int | None, run_budget: int | None, model: str | None,
                    workdir: str | None) -> tuple[list[str], str | None]:
    """Return (argv, cwd). Only flags present in ``info.flags`` are used."""
    require_usable(info)
    f = info.flags
    cmd = [info.binary, "chat"]
    if info.query_file:
        cmd += ["--query-file", query_file]
    else:
        cmd += ["-q" if "-q" in f else "--query", query_text]
    if "-Q" in f:
        cmd.append("-Q")
    elif "--quiet" in f:
        cmd.append("--quiet")
    if "--oneshot" in f:
        cmd.append("--oneshot")
    cmd += ["-t" if "-t" in f else "--toolsets", toolsets]
    if max_turns is not None and "--max-turns" in f:
        cmd += ["--max-turns", str(max_turns)]
    if run_budget is not None and "--run-budget" in f:
        cmd += ["--run-budget", str(run_budget)]
    if model:
        cmd += ["-m" if "-m" in f else "--model", model]
    cwd = None
    if workdir:
        if "--in" in f:
            cmd += ["--in", workdir]
        else:
            cwd = workdir
    return cmd, cwd
