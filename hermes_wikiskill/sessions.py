"""Reading exported Hermes sessions: visibility proof, skill_view receipts, usage.

Facts (research/R1-qa.md A1/A7): ``hermes sessions export --format jsonl`` writes one
full session object per line, including the rendered ``system_prompt``; the skills
index is the ``<available_skills> … </available_skills>`` block with lines
``  <category>: <desc>`` / ``    - <name>: <desc>`` or ``  <category> [names only]: a, b``.
Usage columns: input_tokens, output_tokens, …, estimated_cost_usd, actual_cost_usd.
"""

from __future__ import annotations

import json
import re

_BLOCK = re.compile(r"<available_skills>(.*?)</available_skills>", re.S)
_ITEM = re.compile(r"^\s*-\s+([^\s:]+)\s*(?::|$)")
_NAMES_ONLY = re.compile(r"^\s*(\S+)\s+\[names only\]:\s*(.*)$")
_CATEGORY = re.compile(r"^\s{1,3}([^\s:\-][^\s:]*)\s*:")
_SESSION_ID = re.compile(r"session_id[:\s]+([0-9A-Za-z_.\-]+)")


def load(path: str) -> dict:
    """First JSON object of an exported session JSONL ({} if unreadable)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def session_id_from_text(text: str) -> str | None:
    """`-Q` prints ``session_id: <id>`` on STDERR (qa-H46); take the LAST one (re-synced after compression)."""
    ids = _SESSION_ID.findall(text or "")
    return ids[-1] if ids else None


def available_skills(system_prompt: str | None) -> set[str] | None:
    """Skill names listed in the rendered index; None when there is no index at all."""
    blocks = _BLOCK.findall(system_prompt or "")
    if not blocks:
        return None
    names: set[str] = set()
    for block in blocks:
        for line in block.splitlines():
            m = _NAMES_ONLY.match(line)
            if m:
                names.update(n.strip() for n in m.group(2).split(",") if n.strip())
                continue
            m = _ITEM.match(line)
            if m:
                names.add(m.group(1))
                continue
            m = _CATEGORY.match(line)
            if m:  # a top-level skill is its own category
                names.add(m.group(1))
    return names


def _messages(session: dict) -> list:
    msgs = session.get("messages")
    return msgs if isinstance(msgs, list) else []


def _call_name_args(call: dict) -> tuple[str | None, object]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else call
    name = fn.get("name")
    args = fn.get("arguments", fn.get("args"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    return name, args


def tool_calls(session: dict) -> list[tuple[str, object]]:
    out = []
    for m in _messages(session):
        if not isinstance(m, dict):
            continue
        calls = m.get("tool_calls")
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except ValueError:
                calls = []
        for c in calls or []:
            if isinstance(c, dict):
                name, args = _call_name_args(c)
                if name:
                    out.append((name, args))
    return out


def skill_views(session: dict) -> list[str]:
    """Names passed to ``skill_view`` in this session, in call order."""
    out = []
    for name, args in tool_calls(session):
        if name != "skill_view":
            continue
        if isinstance(args, dict) and args.get("name"):
            out.append(str(args["name"]))
        elif isinstance(args, str):
            out.append(args)
    return out


def tool_call_count(session: dict) -> int:
    n = session.get("tool_call_count")
    if isinstance(n, int):
        return n
    return len(tool_calls(session))


def usage(session: dict) -> dict:
    def num(k):
        v = session.get(k)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    cost = num("actual_cost_usd")
    if cost is None:
        cost = num("estimated_cost_usd")
    return {"input_tokens": num("input_tokens"), "output_tokens": num("output_tokens"),
            "cache_read_tokens": num("cache_read_tokens"),
            "reasoning_tokens": num("reasoning_tokens"),
            "api_call_count": num("api_call_count"),
            "cost_usd": cost, "cost_status": session.get("cost_status"),
            "model": session.get("model")}


def tool_results(session: dict) -> list[tuple[str, str]]:
    """(tool name, result text) for every ``role: tool`` message."""
    out = []
    for m in _messages(session):
        if isinstance(m, dict) and m.get("role") == "tool":
            c = m.get("content")
            text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            out.append((str(m.get("tool_name") or m.get("name") or "tool"), text or ""))
    return out


# F1 (QA): what a redacted VAL/TEST session keeps verbatim; everything else becomes a digest.
REDACT_KEEP_TOP = frozenset({"id", "session_id", "source", "model", "system_prompt", "cost_status",
                             "billing_provider", "billing_mode", "end_reason", "started_at", "ended_at",
                             "parent_session_id", "platform"})
REDACT_KEEP_MSG = frozenset({"role", "tool_name", "tool_call_id", "name", "finish_reason", "timestamp"})
REDACT_KEEP_ARGS = frozenset({"skill_view", "skills_list"})


def digest(value: object) -> str:
    """``sha256:<hex> len=<n>`` of a string (or of the JSON of anything else)."""
    import hashlib

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()} len={len(text)}"


def _redact_value(v: object) -> object:
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str) and not v:
        return v
    return digest(v)


def _redact_call(call: object) -> object:
    if not isinstance(call, dict):
        return _redact_value(call)
    out = {k: call[k] for k in ("id", "call_id", "type") if k in call}
    fn = call.get("function") if isinstance(call.get("function"), dict) else None
    src = fn if fn is not None else call
    name = src.get("name")
    args_key = "arguments" if "arguments" in src else "args"
    red = {"name": name}
    if args_key in src:
        red[args_key] = src[args_key] if name in REDACT_KEEP_ARGS else _redact_value(src[args_key])
    if fn is not None:
        out["function"] = red
    else:
        out.update(red)
    return out


def redact(session: dict) -> dict:
    """VAL/TEST session with every answer-bearing string replaced by :func:`digest`.

    Kept verbatim: system_prompt (skills index), roles, tool names/ids, ``skill_view`` /
    ``skills_list`` arguments, numeric usage/cost columns and the ids/timestamps above.
    Replaced: message contents, every other tool argument, every tool result, reasoning."""
    out: dict = {}
    for k, v in session.items():
        if k == "messages" and isinstance(v, list):
            msgs = []
            for m in v:
                if not isinstance(m, dict):
                    msgs.append(_redact_value(m))
                    continue
                rm = {}
                for mk, mv in m.items():
                    if mk in REDACT_KEEP_MSG and (mv is None or isinstance(mv, (str, int, float, bool))):
                        rm[mk] = mv
                    elif mk == "tool_calls":
                        calls = mv
                        if isinstance(calls, str):
                            try:
                                calls = json.loads(calls)
                            except ValueError:
                                calls = None
                        rm[mk] = ([_redact_call(c) for c in calls] if isinstance(calls, list)
                                  else _redact_value(mv))
                    else:
                        rm[mk] = _redact_value(mv)
                msgs.append(rm)
            out[k] = msgs
        elif k in REDACT_KEEP_TOP and (v is None or isinstance(v, (str, int, float, bool))):
            out[k] = v
        else:
            out[k] = _redact_value(v)
    out["redacted"] = True
    return out
