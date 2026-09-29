"""Plain-text renderers for the Hermes WikiSkill CLI.

The CLI's ``--json`` mode is the machine interface.  This module formats the
same result dictionaries for people, while keeping unknown and partial result
shapes readable as aligned key/value pairs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TITLES = {
    "status": "Workspace status",
    "evolve": "Evolution",
    "gate": "Gate result",
    "run-task": "Task run",
    "eval": "Evaluation",
    "promote": "Skill promotion",
    "demote": "Skill demotion",
    "setup-profile": "Profile setup",
}


def render(kind: str, data: dict) -> str:
    """Render a CLI result as uncolored, human-readable text.

    Missing fields and new result fields are expected as the CLI evolves.  A
    partial known result falls back to aligned key/value output; extra fields
    are retained in a details section rather than discarded.
    """
    values = data if isinstance(data, Mapping) else {"data": data}

    if kind == "version":
        rendered = _render_version(values)
    elif kind == "doctor":
        rendered = _render_doctor(values)
    elif kind == "eval":
        rendered = _render_eval(values)
    elif kind == "setup-profile":
        rendered = _render_setup_profile(values)
    elif kind == "report":
        rendered = _render_report(values)
    elif isinstance(kind, str) and kind in _TITLES:
        rendered = _render_section(_TITLES[kind], values)
    else:
        rendered = _aligned(values)

    return _ANSI_CSI.sub("", rendered)


def _render_version(data: Mapping[Any, Any]) -> str:
    if not data or not {"tool", "version"} <= data.keys():
        return _aligned(data)

    tool = data.get("tool", "hermes-wikiskill")
    version = data.get("version")
    heading = f"{_plain(tool)} { _plain(version) }".strip()
    lines = [heading or "Version"]
    for key, label in (("hermes_version", "Hermes"), ("python", "Python")):
        if key in data:
            lines.append(f"{label}: {_plain(data[key])}")
    return _with_extras(lines, data, {"tool", "version", "hermes_version", "python"})


def _render_doctor(data: Mapping[Any, Any]) -> str:
    checks = data.get("checks")
    if not isinstance(data.get("ok"), bool) or not isinstance(checks, list):
        return _aligned(data)

    lines = ["Doctor: OK" if data["ok"] else "Doctor: FAILED"]
    for check in checks:
        if not isinstance(check, Mapping):
            lines.append(f"  {_plain(check)}")
            continue
        status = _plain(check.get("status", "unknown")).upper()
        check_id = _plain(check.get("id", "check"))
        detail = check.get("detail")
        suffix = f": {_plain(detail)}" if detail not in (None, "") else ""
        lines.append(f"  {status:<4} {check_id}{suffix}")
        extra = {key: value for key, value in check.items() if key not in {"id", "status", "detail"}}
        if extra:
            lines.extend(f"    {line}" for line in _aligned(extra).splitlines())
    return _with_extras(lines, data, {"tool", "version", "ok", "checks"})


def _render_eval(data: Mapping[Any, Any]) -> str:
    repeats = data.get("repeats")
    if not isinstance(repeats, list) or "mean" not in data:
        return _aligned(data)

    skill_mode = data.get("skills", "unknown")
    lines = [f"Evaluation ({_plain(skill_mode)} skills)", f"Mean: {_plain(data.get('mean'))}"]
    for index, repeat in enumerate(repeats, start=1):
        if isinstance(repeat, Mapping):
            rep = repeat.get("rep", index)
            mean = repeat.get("mean", "unknown")
            results = repeat.get("results")
            task_count = len(results) if isinstance(results, list) else "?"
            lines.append(f"  Repeat {rep}: mean={_plain(mean)}, tasks={task_count}")
        else:
            lines.append(f"  Repeat {index}: {_plain(repeat)}")
    hidden = {"repeats", "mean", "skills"}
    return _with_extras(lines, data, hidden)


def _render_setup_profile(data: Mapping[Any, Any]) -> str:
    if "changed" not in data:
        return _render_section(_TITLES["setup-profile"], data)
    state = "changed" if data.get("changed") else "unchanged"
    return _with_extras([f"Profile setup: {state}"], data, {"changed"})


def _render_report(data: Mapping[Any, Any]) -> str:
    report_keys = {"doctor", "acceptance", "evolve", "effectiveness", "costs"}
    if not report_keys & data.keys():
        return _aligned(data)

    def num(value: Any) -> str:
        return "missing" if value is None else _plain(value)

    lines = [f"Report: {_plain(data.get('ws', '?'))}"]

    doctor = data.get("doctor")
    if isinstance(doctor, Mapping) and isinstance(doctor.get("ok"), bool):
        lines.append(f"Doctor: {'OK' if doctor['ok'] else 'FAILED'}")
    else:
        lines.append("Doctor: missing")

    acceptance = data.get("acceptance") if isinstance(data.get("acceptance"), Mapping) else {}
    lines.append("Acceptance:")
    for target in ("stock", "current", "framework"):
        doc = acceptance.get(target)
        summary = doc.get("summary") if isinstance(doc, Mapping) else None
        if isinstance(summary, Mapping):
            lines.append(f"  {target:<9} pass={num(summary.get('pass'))} fail={num(summary.get('fail'))} "
                         f"skip={num(summary.get('skip'))}")
        else:
            lines.append(f"  {target:<9} missing")

    evolve = data.get("evolve")
    if isinstance(evolve, Mapping):
        history = evolve.get("history") if isinstance(evolve.get("history"), list) else []
        accepted = sum(1 for h in history if isinstance(h, Mapping) and h.get("accepted") is True)
        lines.append(f"Evolve: baseline={num(evolve.get('baseline'))} r_best={num(evolve.get('r_best'))} "
                     f"iterations={len(history)} accepted={accepted}")
    else:
        lines.append("Evolve: missing")

    eff = data.get("effectiveness") if isinstance(data.get("effectiveness"), Mapping) else {}
    evals = eff.get("evals") if isinstance(eff.get("evals"), list) else []
    lines.append(f"Effectiveness: empty={num(eff.get('empty_mean'))} evolved={num(eff.get('active_mean'))} "
                 f"delta={num(eff.get('delta'))} evals={len(evals)}")

    costs = data.get("costs") if isinstance(data.get("costs"), Mapping) else {}
    total = costs.get("total_usd")
    lines.append(f"Costs: total_usd={num(total)} runs={num(costs.get('runs'))} "
                 f"runs_without_cost={num(costs.get('runs_without_cost'))}")

    notes = data.get("notes")
    if isinstance(notes, list) and notes:
        lines.append("Notes:")
        lines.extend(f"  {_plain(note)}" for note in notes)
    return "\n".join(lines)


def _render_section(title: str, data: Mapping[Any, Any]) -> str:
    if not data:
        return _aligned(data)
    return f"{title}\n{_aligned(data)}"


def _with_extras(lines: list[str], data: Mapping[Any, Any], known: set[str]) -> str:
    extras = {key: value for key, value in data.items() if key not in known}
    if extras:
        lines.extend(("Details:", _aligned(extras)))
    return "\n".join(lines)


def _aligned(data: Mapping[Any, Any]) -> str:
    """Format any mapping without assuming a particular set of keys."""
    if not data:
        return "(no data)"
    pairs = sorted(((str(key), value) for key, value in data.items()), key=lambda pair: pair[0])
    width = max(len(key) for key, _ in pairs)
    return "\n".join(f"{key:<{width}} : {_plain(value)}" for key, value in pairs)


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        try:
            return str(value)
        except Exception:
            return "<unprintable>"
