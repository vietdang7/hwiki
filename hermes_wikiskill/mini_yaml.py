"""A tiny YAML subset reader (stdlib-only): nested mappings, scalars, simple lists.

Enough to read the few Hermes config keys the framework needs (``model:``,
``providers:``, ``agent.disabled_toolsets``). Anything it cannot parse is skipped,
never guessed. The framework WRITES its own configs with ``dump``.
"""

from __future__ import annotations

import re

_KEY = re.compile(r"^([A-Za-z0-9_.\-]+|\"[^\"]*\"|'[^']*')\s*:(?:\s+(.*))?$")


def _scalar(raw: str):
    s = raw.strip()
    if not s:
        return None
    if " #" in s and not s.startswith(("'", '"')):
        s = s.split(" #", 1)[0].rstrip()
    if (s[0] == s[-1]) and s[0] in "'\"" and len(s) >= 2:
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def loads(text: str) -> dict:
    root: dict = {}
    # stack of (indent, container)
    stack: list[tuple[int, object]] = [(-1, root)]
    pending_key: tuple[dict, str] | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or raw.strip() in ("---", "..."):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        while stack and indent <= stack[-1][0] and len(stack) > 1:
            stack.pop()
        parent = stack[-1][1]
        if pending_key is not None:
            pdict, pkey = pending_key
            pending_key = None
            container: object = [] if line.startswith("- ") or line == "-" else {}
            pdict[pkey] = container
            stack.append((indent - 1, container))
            parent = container
        if line.startswith("-"):
            if isinstance(parent, list):
                parent.append(_scalar(line[1:]))
            continue
        m = _KEY.match(line)
        if not m or not isinstance(parent, dict):
            continue
        key = m.group(1).strip("\"'")
        val = m.group(2)
        if val is None or val.strip() == "" or val.strip().startswith("#"):
            parent[key] = None
            pending_key = (parent, key)
            stack.append((indent, parent))
        else:
            parent[key] = _scalar(val)
    return root


def _fmt(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{},&*!|>'\"%@`]|^\s|\s$", s) or s.lower() in (
            "true", "false", "yes", "no", "null", "~"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dumps(data: dict, indent: int = 0) -> str:
    out = []
    pad = " " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out.append(dumps(v, indent + 2).rstrip("\n")) if v else out.append(f"{pad}  {{}}")
        elif isinstance(v, list):
            if not v:
                out.append(f"{pad}{k}: []")
            else:
                out.append(f"{pad}{k}:")
                out.extend(f"{pad}  - {_fmt(x)}" for x in v)
        else:
            out.append(f"{pad}{k}: {_fmt(v)}")
    return "\n".join(out) + "\n"
