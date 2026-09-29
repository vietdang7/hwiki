"""Workspace report: gather the run artifacts into one dict, render it as HTML.

``build`` only reads files (state, eval summaries, acceptance results, trace
metadata); it never writes and never invents a number.  A missing or unreadable
artifact becomes ``None`` plus a ``notes`` entry, which ``render_html`` shows.
``render_html`` returns one self-contained page: inline CSS/JS, no external
references, light and dark themes, readable at phone width.
"""

from __future__ import annotations

import glob
import html
import json
import math
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

DEFAULT_ACCEPTANCE_DIR = "~/ops/hws-acceptance/results"
TARGETS = ("stock", "current", "framework")
COST_SOURCES = ("traces", "runs", "eval")
# Rollout tags whose run metadata is not already covered by a trace meta.
_RUN_TAG_PREFIXES = ("maintain-", "propose-")


# --------------------------------------------------------------------------- build


def build(ws: str, *, acceptance_dir: str | None = None, doctor: dict | None = None,
          eval_dir: str | None = None) -> dict:
    """Collect every report number from the workspace's artifacts (read-only)."""
    ws_path = os.path.abspath(os.path.expanduser(ws))
    acc_dir = os.path.abspath(os.path.expanduser(acceptance_dir or DEFAULT_ACCEPTANCE_DIR))
    eval_root = os.path.abspath(os.path.expanduser(eval_dir)) if eval_dir else os.path.join(ws_path, "eval")
    notes: list[str] = []

    if not os.path.isdir(ws_path):
        notes.append(f"missing: {ws_path}")

    evals = _load_evals(eval_root, notes)
    return {
        "generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ws": ws_path,
        "doctor": _doctor(doctor, notes),
        "acceptance": {t: _load_json(os.path.join(acc_dir, f"{t}.json"), notes) for t in TARGETS},
        "evolve": _evolve(ws_path, notes),
        "effectiveness": _effectiveness(evals, notes),
        "costs": _costs(ws_path, evals, notes),
        "notes": notes,
    }


def _load_json(path: str, notes: list[str]) -> dict | None:
    if not os.path.isfile(path):
        notes.append(f"missing: {path}")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        notes.append(f"unreadable: {path} ({exc.__class__.__name__})")
        return None
    if not isinstance(doc, dict):
        notes.append(f"unreadable: {path} (not a JSON object)")
        return None
    return doc


def _doctor(doctor: Any, notes: list[str]) -> dict | None:
    if doctor is None:
        notes.append("missing: doctor result (not provided)")
        return None
    if not isinstance(doctor, Mapping):
        notes.append("unreadable: doctor result (not a JSON object)")
        return None
    return dict(doctor)


def _evolve(ws: str, notes: list[str]) -> dict | None:
    state = _load_json(os.path.join(ws, "runs", "state.json"), notes)
    if state is None:
        return None
    raw_history = state.get("history")
    history = [dict(h) for h in raw_history if isinstance(h, Mapping)] if isinstance(raw_history, list) else []
    return {
        "domain": state.get("domain"),
        "baseline": state.get("baseline"),
        "r_best": state.get("r_best"),
        "next_iter": state.get("next_iter"),
        "history": history,
        "accepted_iters": [h.get("iter") for h in history if h.get("accepted") is True],
        "active_skills": _active_skills(ws),
    }


def _active_skills(ws: str) -> list[str] | None:
    active = os.path.join(ws, "skills", "active")
    if not os.path.isdir(active):
        return None
    return sorted(name for name in os.listdir(active)
                  if os.path.isfile(os.path.join(active, name, "SKILL.md")))


def _load_evals(eval_root: str, notes: list[str]) -> list[dict]:
    if not os.path.isdir(eval_root):
        notes.append(f"missing: {eval_root}")
        return []
    evals = []
    for path in sorted(glob.glob(os.path.join(eval_root, "*", "summary.json"))):
        doc = _load_json(path, notes)
        if doc is not None:
            evals.append({**doc, "summary_path": path})
    if not evals:
        notes.append(f"missing: {os.path.join(eval_root, '*', 'summary.json')}")
    return evals


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _repeat_means(summary: Mapping[str, Any]) -> list[float]:
    repeats = summary.get("repeats")
    if isinstance(repeats, list) and repeats:
        return [r["mean"] for r in repeats if isinstance(r, Mapping) and _is_num(r.get("mean"))]
    return [summary["mean"]] if _is_num(summary.get("mean")) else []


def _results(summary: Mapping[str, Any]):
    repeats = summary.get("repeats")
    for rep in repeats if isinstance(repeats, list) else []:
        if not isinstance(rep, Mapping) or not isinstance(rep.get("results"), list):
            continue
        for res in rep["results"]:
            if isinstance(res, Mapping):
                yield rep, res


def _effectiveness(evals: list[dict], notes: list[str]) -> dict:
    groups: dict[str, dict] = {}
    for summary in evals:
        key = str(summary.get("tasks_sha256") or summary.get("tasks_file") or "unknown")
        group = groups.setdefault(key, {
            "tasks_sha256": summary.get("tasks_sha256"), "tasks_file": summary.get("tasks_file"),
            "tags": {"empty": [], "active": []}, "repeat_means": {"empty": [], "active": []},
            "scores": {}, "latest": "",
        })
        kind = summary.get("skills")
        if kind not in ("empty", "active"):
            continue
        group["tags"][kind].append(summary.get("tag"))
        group["repeat_means"][kind].extend(_repeat_means(summary))
        group["latest"] = max(group["latest"], str(summary.get("finished") or ""))
        for _, res in _results(summary):
            per_task = group["scores"].setdefault(str(res.get("task_id")), {"empty": [], "active": []})
            per_task[kind].append(res.get("score") if _is_num(res.get("score")) else None)

    out_groups = []
    for group in groups.values():
        empty_mean = _mean(group["repeat_means"]["empty"])
        active_mean = _mean(group["repeat_means"]["active"])
        tasks = []
        for task_id in sorted(group["scores"]):
            scores = group["scores"][task_id]
            e = _mean([s for s in scores["empty"] if s is not None])
            a = _mean([s for s in scores["active"] if s is not None])
            tasks.append({"task_id": task_id, "empty_scores": scores["empty"], "active_scores": scores["active"],
                          "empty_mean": e, "active_mean": a,
                          "delta": round(a - e, 6) if e is not None and a is not None else None})
        out_groups.append({
            "tasks_sha256": group["tasks_sha256"], "tasks_file": group["tasks_file"],
            "empty_tags": group["tags"]["empty"], "active_tags": group["tags"]["active"],
            "empty_repeat_means": group["repeat_means"]["empty"],
            "active_repeat_means": group["repeat_means"]["active"],
            "empty_mean": empty_mean, "active_mean": active_mean,
            "delta": round(active_mean - empty_mean, 6) if empty_mean is not None and active_mean is not None else None,
            "tasks": tasks, "_latest": group["latest"],
        })

    paired = [g for g in out_groups if g["delta"] is not None]
    primary = max(paired, key=lambda g: (len(g["empty_repeat_means"]) + len(g["active_repeat_means"]), g["_latest"]),
                  default=None)
    for g in out_groups:
        g.pop("_latest")
    if evals and primary is None:
        notes.append("missing: no task set has both an empty-skills and an active-skills eval")
    return {
        "evals": evals,
        "empty_mean": primary["empty_mean"] if primary else None,
        "active_mean": primary["active_mean"] if primary else None,
        "delta": primary["delta"] if primary else None,
        "basis": {"tasks_sha256": primary["tasks_sha256"], "tasks_file": primary["tasks_file"]} if primary else None,
        "groups": out_groups,
    }


def _usage_cost(doc: Any) -> float | None:
    """Recorded ``usage.cost_usd`` of one rollout record, or None when not recorded."""
    usage = doc.get("usage") if isinstance(doc, Mapping) else None
    cost = usage.get("cost_usd") if isinstance(usage, Mapping) else None
    return float(cost) if _is_num(cost) and cost >= 0 else None


def _costs(ws: str, evals: list[dict], notes: list[str]) -> dict:
    acc = {src: {"total_usd": 0.0, "runs": 0, "runs_without_cost": 0} for src in COST_SOURCES}

    def add(src: str, cost: float | None) -> None:
        acc[src]["runs"] += 1
        if cost is None:
            acc[src]["runs_without_cost"] += 1
        else:
            acc[src]["total_usd"] += cost

    for path in sorted(glob.glob(os.path.join(ws, "raw", "traces", "**", "*.meta.json"), recursive=True)):
        meta = _load_json(path, notes)
        if meta is not None:
            add("traces", _usage_cost(meta))

    runs_dir = os.path.join(ws, "runs")
    # maintain-NN / propose-NN, recursively: the final run.json AND every kept attempt-<k>/run.json (INTERFACE §6)
    for path in sorted(glob.glob(os.path.join(runs_dir, "**", "*.json"), recursive=True)):
        tag = os.path.relpath(path, runs_dir).split(os.sep)[0]
        if not tag.startswith(_RUN_TAG_PREFIXES):
            continue
        doc = _load_json(path, notes)
        if doc is not None and "usage" in doc:
            add("runs", _usage_cost(doc))

    for summary in evals:
        for _, res in _results(summary):
            add("eval", _usage_cost(res))

    by_source = {}
    for src, a in acc.items():
        known = a["runs"] - a["runs_without_cost"]
        by_source[src] = {"total_usd": round(a["total_usd"], 6) if known else None,
                          "runs": a["runs"], "runs_without_cost": a["runs_without_cost"]}
    runs = sum(a["runs"] for a in acc.values())
    without = sum(a["runs_without_cost"] for a in acc.values())
    total = round(sum(a["total_usd"] for a in acc.values()), 6) if runs - without else (0.0 if runs == 0 else None)
    return {"total_usd": total, "runs": runs, "runs_without_cost": without, "by_source": by_source}


# --------------------------------------------------------------------------- html


def _esc(value: Any) -> str:
    """HTML-escape text; a double slash is split with <wbr> so no URL-like literal survives."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return html.escape(text, quote=True).replace("//", "/<wbr>/")


def _num(value: Any, digits: int = 4, signed: bool = False) -> str:
    if not _is_num(value):
        return '<span class="na">&mdash;</span>' if value is None else _esc(value)
    if isinstance(value, int):
        text = str(value)
    else:
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        text = "0" if text in ("-0", "") else text
    if signed and value > 0:
        text = "+" + text
    return text


def _usd(value: Any) -> str:
    return f"${value:.4f}" if _is_num(value) else '<span class="na">&mdash;</span>'


_STATUS_CLASS = {"pass": "ok", "ok": "ok", "accept": "ok", "fail": "bad", "failed": "bad", "reject": "bad",
                 "warn": "warn", "skip": "muted", "n/a": "muted", "missing": "muted"}


def _badge(status: Any) -> str:
    label = str(status) if status not in (None, "") else "missing"
    return f'<span class="badge {_STATUS_CLASS.get(label.lower(), "muted")}">{_esc(label.upper())}</span>'


def _section(sid: str, title: str, body: str, lead: str = "") -> str:
    lead_html = f'<p class="lead">{lead}</p>' if lead else ""
    return f'<section id="{sid}" aria-labelledby="{sid}-h"><h2 id="{sid}-h">{_esc(title)}</h2>{lead_html}{body}</section>'


def _missing(what: str) -> str:
    return f'<p class="missing">Missing: {_esc(what)} &mdash; no number shown rather than a guessed one.</p>'


def _table(head: list[str], rows: list[str], caption: str = "") -> str:
    cap = f"<caption>{_esc(caption)}</caption>" if caption else ""
    ths = "".join(f'<th scope="col">{h}</th>' for h in head)
    return f'<div class="scroll"><table>{cap}<thead><tr>{ths}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _kv(pairs: list[tuple[str, str]]) -> str:
    return '<dl class="kv">' + "".join(f"<div><dt>{_esc(k)}</dt><dd>{v}</dd></div>" for k, v in pairs) + "</dl>"


def _tile(label: str, value: str, sub: str = "", tone: str = "") -> str:
    return (f'<div class="tile {tone}"><div class="tile-label">{_esc(label)}</div>'
            f'<div class="tile-value">{value}</div><div class="tile-sub">{sub}</div></div>')


def _tiles(data: Mapping[str, Any]) -> str:
    doctor = data.get("doctor")
    if isinstance(doctor, Mapping) and isinstance(doctor.get("ok"), bool):
        d = _tile("Doctor", "OK" if doctor["ok"] else "FAILED",
                  f'{len(doctor.get("checks") or [])} checks', "ok" if doctor["ok"] else "bad")
    else:
        d = _tile("Doctor", "&mdash;", "missing", "muted")

    fw = (data.get("acceptance") or {}).get("framework")
    summ = fw.get("summary") if isinstance(fw, Mapping) else None
    if isinstance(summ, Mapping):
        fail = summ.get("fail")
        a = _tile("Framework acceptance", f'{_num(summ.get("pass"))} pass',
                  f'{_num(fail)} fail &middot; {_num(summ.get("skip"))} skip',
                  "ok" if fail == 0 else "bad")
    else:
        a = _tile("Framework acceptance", "&mdash;", "missing", "muted")

    ev = data.get("evolve")
    if isinstance(ev, Mapping):
        e = _tile("Evolve R_best", _num(ev.get("r_best")),
                  f'baseline {_num(ev.get("baseline"))} &middot; {len(ev.get("accepted_iters") or [])} accepted')
    else:
        e = _tile("Evolve R_best", "&mdash;", "missing", "muted")

    eff = data.get("effectiveness") or {}
    delta = eff.get("delta")
    x = _tile("Held-out \u0394", _num(delta, signed=True),
              f'empty {_num(eff.get("empty_mean"))} &rarr; evolved {_num(eff.get("active_mean"))}',
              "muted" if not _is_num(delta) or delta == 0 else ("ok" if delta > 0 else "bad"))

    costs = data.get("costs") or {}
    c = _tile("LLM cost", _usd(costs.get("total_usd")),
              f'{_num(costs.get("runs"))} runs &middot; {_num(costs.get("runs_without_cost"))} without cost')
    return f'<div class="tiles">{d}{a}{e}{x}{c}</div>'


def _doctor_html(doctor: Any) -> str:
    if not isinstance(doctor, Mapping):
        return _missing("doctor result")
    head = _kv([("Result", _badge("ok" if doctor.get("ok") else "fail") if isinstance(doctor.get("ok"), bool)
                 else _badge(None)),
                ("Tool", _esc(doctor.get("tool") or "?")), ("Version", _esc(doctor.get("version") or "?"))])
    rows = []
    for check in doctor.get("checks") or []:
        if isinstance(check, Mapping):
            rows.append(f'<tr><th scope="row"><code>{_esc(check.get("id", "?"))}</code></th>'
                        f'<td>{_badge(check.get("status"))}</td><td>{_esc(check.get("detail", ""))}</td></tr>')
    return head + (_table(["Check", "Status", "Detail"], rows) if rows else '<p class="missing">No checks recorded.</p>')


def _acceptance_html(acceptance: Any) -> str:
    acceptance = acceptance if isinstance(acceptance, Mapping) else {}
    docs = {t: acceptance.get(t) if isinstance(acceptance.get(t), Mapping) else None for t in TARGETS}
    order: list[str] = []
    names: dict[str, str] = {}
    cells: dict[tuple[str, str], Mapping] = {}
    for target in ("framework", "stock", "current"):
        doc = docs[target]
        for check in (doc or {}).get("checks") or []:
            if not isinstance(check, Mapping):
                continue
            cid = str(check.get("id", "?"))
            if cid not in names:
                order.append(cid)
                names[cid] = str(check.get("name") or "")
            cells[(cid, target)] = check
    if not order:
        return _missing("acceptance results for stock, current and framework")

    head = ["Check"]
    for target in TARGETS:
        doc = docs[target]
        if doc is None:
            head.append(f'{_esc(target)}<span class="th-sub">missing</span>')
            continue
        s = doc.get("summary") if isinstance(doc.get("summary"), Mapping) else {}
        head.append(f'{_esc(target)}<span class="th-sub">{_num(s.get("pass"))} pass &middot; '
                    f'{_num(s.get("fail"))} fail &middot; {_num(s.get("skip"))} skip</span>')
    rows = []
    for cid in sorted(order):
        tds = []
        for target in TARGETS:
            check = cells.get((cid, target))
            if check is None:
                tds.append('<td class="cell"><span class="na">&mdash;</span></td>')
                continue
            ev = check.get("evidence")
            if ev in (None, ""):
                tds.append(f'<td class="cell">{_badge(check.get("status"))}</td>')
            else:
                tds.append(f'<td class="cell"><details><summary>{_badge(check.get("status"))}</summary>'
                           f'<div class="evidence">{_esc(ev)}</div></details></td>')
        rows.append(f'<tr><th scope="row"><code>{_esc(cid)}</code><span class="th-sub">{_esc(names[cid])}</span></th>'
                    f'{"".join(tds)}</tr>')
    meta = []
    for target in TARGETS:
        doc = docs[target]
        if doc is not None:
            meta.append(f'<li><strong>{_esc(target)}</strong>: host {_esc(doc.get("host", "?"))}, '
                        f'Hermes {_esc(doc.get("hermes_version", "?"))}, {_esc(doc.get("timestamp", "?"))}</li>')
    return (_table(head, rows, "Same 18 check ids against each target; AC01/04/07/08/12 use target-specific "
                               "procedures and AC10/15/17 on stock/current only test that the feature exists. "
                               "Open a status for its evidence.")
            + (f'<ul class="meta">{"".join(meta)}</ul>' if meta else ""))


def _evolve_chart(ev: Mapping[str, Any]) -> str:
    history = [h for h in ev.get("history") or [] if _is_num(h.get("r_val"))]
    baseline = ev.get("baseline")
    points = ([(0, baseline, "base", "iter 0 baseline")] if _is_num(baseline) else []) + [
        (h.get("iter") if _is_num(h.get("iter")) else i + 1, h["r_val"],
         "ok" if h.get("accepted") is True else ("bad" if h.get("accepted") is False else "muted"),
         f'iter {h.get("iter")}: R_val {_num(h["r_val"])} '
         + ("ACCEPT" if h.get("accepted") is True else "reject" if h.get("accepted") is False else "no decision"))
        for i, h in enumerate(history)]
    if not points:
        return ""
    w, h_, left, right, top, bottom = 640, 240, 44, 16, 16, 34
    xs = [p[0] for p in points]
    x_max = max(max(xs), 1)
    y_max = max(1.0, *(p[1] for p in points))

    def px(x: float) -> float:
        return left + (w - left - right) * (x / x_max)

    def py(y: float) -> float:
        return top + (h_ - top - bottom) * (1 - y / y_max)

    grid = []
    for i in range(5):
        y = y_max * i / 4
        grid.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{py(y):.1f}" y2="{py(y):.1f}"/>'
                    f'<text class="axis" x="{left - 6}" y="{py(y) + 4:.1f}" text-anchor="end">{_num(y, 2)}</text>')
    ticks = "".join(f'<text class="axis" x="{px(x):.1f}" y="{h_ - 12}" text-anchor="middle">{int(x)}</text>'
                    for x in sorted(set(xs)))
    # R_best as a step line: it only moves on an accepted iteration.
    best, steps = None, []
    for x, y, tone, _ in points:
        if tone == "base" or (tone == "ok" and (best is None or y > best)):
            best = y
        if best is not None:
            steps.append((px(x), py(best)))
    step_path = ""
    if steps:
        d = f"M{steps[0][0]:.1f},{steps[0][1]:.1f}"
        for (x0, y0), (x1, y1) in zip(steps, steps[1:]):
            d += f" H{x1:.1f} V{y1:.1f}"
        step_path = f'<path class="best" d="{d}"/>'
    line = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y, _, _ in points)
    dots = "".join(f'<circle class="dot {tone}" cx="{px(x):.1f}" cy="{py(y):.1f}" r="5"><title>{_esc(label)}</title></circle>'
                   for x, y, tone, label in points)
    return (f'<figure class="chart"><svg viewBox="0 0 {w} {h_}" role="img" '
            f'aria-label="Validation score per iteration: baseline, R_val and R_best">'
            f'{"".join(grid)}{ticks}{step_path}<polyline class="rval" points="{line}"/>{dots}</svg>'
            '<figcaption><span class="key rval-k"></span>R_val per iteration '
            '<span class="key best-k"></span>R_best '
            '<span class="key dot-ok"></span>accepted <span class="key dot-bad"></span>rejected '
            '(x = iteration, 0 = empty-skill baseline)</figcaption></figure>')


def _evolve_html(ev: Any) -> str:
    if not isinstance(ev, Mapping):
        return _missing("runs/state.json")
    skills = ev.get("active_skills")
    skills_html = ("none (S0)" if skills == [] else '<span class="na">&mdash;</span>' if skills is None
                   else ", ".join(f"<code>{_esc(s)}</code>" for s in skills))
    head = _kv([("Baseline (S0)", _num(ev.get("baseline"))), ("R_best", _num(ev.get("r_best"))),
                ("Next iteration", _num(ev.get("next_iter"))), ("Active skills", skills_html)])
    known = {"iter", "train_mean", "r_val", "accepted", "proposal", "r_best_before", "skills_visible", "ts"}
    rows = []
    for h in ev.get("history") or []:
        verdict = "accept" if h.get("accepted") is True else "reject" if h.get("accepted") is False else "no decision"
        vis = h.get("skills_visible")
        extra = {k: v for k, v in h.items() if k not in known}
        extra_html = (f'<details><summary>more</summary><div class="evidence">{_esc(extra)}</div></details>'
                      if extra else "")
        rows.append(f'<tr><th scope="row">{_num(h.get("iter"))}</th><td>{_num(h.get("train_mean"))}</td>'
                    f'<td>{_num(h.get("r_best_before"))}</td><td>{_num(h.get("r_val"))}</td><td>{_badge(verdict)}</td>'
                    f'<td>{"yes" if vis is True else "no" if vis is False else "&mdash;"}</td>'
                    f'<td class="wrap">{_esc(h.get("proposal") or "")}{extra_html}</td><td>{_esc(h.get("ts") or "")}</td></tr>')
    table = (_table(["Iter", "Train mean", "R_best before", "R_val", "Gate", "Skills visible", "Proposal", "Time"], rows)
             if rows else '<p class="missing">No gated iterations recorded yet.</p>')
    return head + _evolve_chart(ev) + table


def _bar_rows(label: str, values: list[Any], tone: str) -> str:
    out = []
    for i, v in enumerate(values, 1):
        width = max(0.0, min(1.0, float(v))) * 100 if _is_num(v) else 0.0
        out.append(f'<div class="bar-row"><span class="bar-label">{_esc(label)} rep {i}</span>'
                   f'<span class="bar-track"><span class="bar {tone}" style="width:{width:.1f}%"></span></span>'
                   f'<span class="bar-val">{_num(v)}</span></div>')
    return "".join(out)


def _scores(values: Any) -> str:
    return ", ".join(_num(v) for v in values) if isinstance(values, list) and values else "&mdash;"


def _effectiveness_html(eff: Any) -> str:
    eff = eff if isinstance(eff, Mapping) else {}
    evals = [e for e in eff.get("evals") or [] if isinstance(e, Mapping)]
    if not evals:
        return _missing("eval summaries (<ws>/eval/*/summary.json)")
    basis = eff.get("basis") if isinstance(eff.get("basis"), Mapping) else None
    groups = [g for g in eff.get("groups") or [] if isinstance(g, Mapping)]
    primary = next((g for g in groups if basis and g.get("tasks_sha256") == basis.get("tasks_sha256")
                    and g.get("tasks_file") == basis.get("tasks_file")), None)
    parts = [_kv([("Empty skills mean", _num(eff.get("empty_mean"))),
                  ("Evolved skills mean", _num(eff.get("active_mean"))),
                  ("\u0394 (evolved \u2212 empty)", _num(eff.get("delta"), signed=True)),
                  ("Task set", _esc((basis or {}).get("tasks_file") or "?"))])]
    if primary:
        parts.append('<div class="bars" role="img" aria-label="Mean score per repeat, empty versus evolved skills">'
                     + _bar_rows("empty", primary.get("empty_repeat_means") or [], "muted")
                     + _bar_rows("evolved", primary.get("active_repeat_means") or [], "accent") + "</div>")

    rows = []
    for e in evals:
        repeats = e.get("repeats") if isinstance(e.get("repeats"), list) else []
        rep_html = ", ".join(_num(r.get("mean")) for r in repeats if isinstance(r, Mapping)) or "&mdash;"
        rows.append(f'<tr><th scope="row"><code>{_esc(e.get("tag", "?"))}</code></th><td>{_esc(e.get("skills", "?"))}</td>'
                    f'<td class="wrap">{_esc(", ".join(map(str, e.get("skill_names") or [])) or "none")}</td>'
                    f'<td>{rep_html}</td><td>{_num(e.get("mean"))}</td><td>{_esc(e.get("model") or "")}</td>'
                    f'<td>{_num(e.get("max_turns"))}</td><td class="wrap">{_esc(e.get("tasks_file") or "")}</td></tr>')
    parts.append(_table(["Eval tag", "Skills", "Skill names", "Repeat means", "Mean", "Model", "Max turns", "Tasks"],
                        rows, "Every eval summary found"))

    if primary and primary.get("tasks"):
        trows = []
        for t in primary["tasks"]:
            trows.append(f'<tr><th scope="row"><code>{_esc(t.get("task_id"))}</code></th>'
                         f'<td>{_scores(t.get("empty_scores"))}</td><td>{_num(t.get("empty_mean"))}</td>'
                         f'<td>{_scores(t.get("active_scores"))}</td><td>{_num(t.get("active_mean"))}</td>'
                         f'<td>{_num(t.get("delta"), signed=True)}</td></tr>')
        parts.append(_table(["Task", "Empty scores", "Empty mean", "Evolved scores", "Evolved mean", "&Delta;"], trows,
                            "Per task, all repeats (held-out set above)"))
    return "".join(parts)


def _costs_html(costs: Any) -> str:
    if not isinstance(costs, Mapping):
        return _missing("cost data")
    rows = []
    by_source = costs.get("by_source") if isinstance(costs.get("by_source"), Mapping) else {}
    labels = {"traces": "Task rollouts (trace metas)", "runs": "Maintainer / proposer runs (incl. retried attempts)", "eval": "Held-out evals"}
    for src, a in by_source.items():
        if isinstance(a, Mapping):
            rows.append(f'<tr><th scope="row">{_esc(labels.get(src, src))}</th><td>{_num(a.get("runs"))}</td>'
                        f'<td>{_num(a.get("runs_without_cost"))}</td><td>{_usd(a.get("total_usd"))}</td></tr>')
    rows.append(f'<tr class="total"><th scope="row">Total</th><td>{_num(costs.get("runs"))}</td>'
                f'<td>{_num(costs.get("runs_without_cost"))}</td><td>{_usd(costs.get("total_usd"))}</td></tr>')
    return (_table(["Source", "Runs", "Runs without cost", "Cost (USD)"], rows)
            + '<p class="lead">Sum of recorded <code>usage.cost_usd</code> only; runs without a recorded cost are '
              'counted, not estimated.</p>')


def _json_island(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    for raw, safe in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("/", "\\/")):
        text = text.replace(raw, safe)
    return f'<script type="application/json" id="hws-data">{text}</script>'


_CSS = """
:root{--bg:#f6f7f9;--surface:#fff;--text:#1b1f24;--muted:#5b6470;--line:#dde1e6;--accent:#2f5bd3;
--ok:#1a7f45;--ok-bg:#e3f4ea;--bad:#b42318;--bad-bg:#fde8e6;--warn:#8a5a00;--warn-bg:#fdf1d8;
--mute-bg:#eceef1;--code:#eef0f3;color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0f1216;--surface:#171b21;--text:#e6e9ee;
--muted:#9aa4b1;--line:#2a3039;--accent:#7ea2ff;--ok:#5fd08e;--ok-bg:#12301f;--bad:#ff8a80;--bad-bg:#3a1714;
--warn:#f2c14e;--warn-bg:#352a0e;--mute-bg:#232931;--code:#222831;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1216;--surface:#171b21;--text:#e6e9ee;--muted:#9aa4b1;--line:#2a3039;
--accent:#7ea2ff;--ok:#5fd08e;--ok-bg:#12301f;--bad:#ff8a80;--bad-bg:#3a1714;--warn:#f2c14e;--warn-bg:#352a0e;
--mute-bg:#232931;--code:#222831;color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,
"Helvetica Neue",Arial,sans-serif}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
header.top{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;justify-content:space-between;margin-bottom:20px}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:1.15rem;margin:0 0 12px}
.sub{color:var(--muted);margin:0;overflow-wrap:anywhere}
code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;
border-radius:4px;overflow-wrap:anywhere}
button.theme{font:inherit;font-size:.9rem;color:var(--text);background:var(--surface);border:1px solid var(--line);
border-radius:8px;padding:6px 12px;cursor:pointer}
button.theme:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
section{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px;margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;
padding:12px 14px}
.tile.ok{border-left-color:var(--ok)}.tile.bad{border-left-color:var(--bad)}.tile.muted{border-left-color:var(--muted)}
.tile-label{color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.tile-value{font-size:1.45rem;font-weight:650;font-variant-numeric:tabular-nums;margin:2px 0}
.tile-sub{color:var(--muted);font-size:.85rem}
.notes{border-color:var(--warn);background:var(--warn-bg)}
.notes ul{margin:0;padding-left:20px}.notes li{overflow-wrap:anywhere}
.lead{color:var(--muted);margin:-4px 0 14px;font-size:.9rem}
.scroll+.lead{margin:10px 0 0}
.missing{color:var(--muted);font-style:italic;margin:0}
.na{color:var(--muted)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px 16px;margin:0 0 14px}
.kv div{min-width:0}.kv dt{color:var(--muted);font-size:.8rem}.kv dd{margin:0;font-weight:600;
font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 0 18px}
.scroll:last-child{margin-bottom:0}
table{border-collapse:collapse;width:100%;font-size:.9rem;font-variant-numeric:tabular-nums}
caption{text-align:left;color:var(--muted);font-size:.85rem;padding:0 0 6px}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line)}
thead th{font-size:.8rem;color:var(--muted);font-weight:600;white-space:nowrap}
tbody th{font-weight:500;white-space:nowrap}
tr.total th,tr.total td{font-weight:700}
.th-sub{display:block;color:var(--muted);font-size:.78rem;font-weight:400;white-space:normal}
td.wrap{min-width:180px;overflow-wrap:anywhere}
.badge{display:inline-block;font-size:.72rem;font-weight:700;letter-spacing:.03em;padding:2px 7px;border-radius:999px;
background:var(--mute-bg);color:var(--muted);white-space:nowrap}
.badge.ok{background:var(--ok-bg);color:var(--ok)}.badge.bad{background:var(--bad-bg);color:var(--bad)}
.badge.warn{background:var(--warn-bg);color:var(--warn)}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
details[open] summary{margin-bottom:6px}
.evidence{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;
background:var(--code);border-radius:6px;padding:6px 8px;max-width:420px}
.cell{min-width:90px}
ul.meta{color:var(--muted);font-size:.85rem;margin:10px 0 0;padding-left:18px}
.chart{margin:0 0 14px}.chart svg{width:100%;height:auto;display:block}
.chart .grid{stroke:var(--line);stroke-width:1}.chart .axis{fill:var(--muted);font-size:11px}
.chart .rval{fill:none;stroke:var(--accent);stroke-width:2}
.chart .best{fill:none;stroke:var(--ok);stroke-width:2;stroke-dasharray:6 4}
.chart .dot{stroke:var(--surface);stroke-width:2}.chart .dot.ok{fill:var(--ok)}.chart .dot.bad{fill:var(--bad)}
.chart .dot.base,.chart .dot.muted{fill:var(--muted)}
figcaption{color:var(--muted);font-size:.82rem;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.key{display:inline-block;width:14px;height:4px;border-radius:2px;margin-right:4px;vertical-align:middle}
.rval-k{background:var(--accent)}.best-k{background:var(--ok)}
.dot-ok,.dot-bad{width:10px;height:10px;border-radius:50%}.dot-ok{background:var(--ok)}.dot-bad{background:var(--bad)}
.bars{margin:0 0 14px;display:grid;gap:6px}
.bar-row{display:grid;grid-template-columns:120px 1fr 56px;gap:10px;align-items:center;font-size:.85rem}
.bar-label{color:var(--muted)}.bar-val{text-align:right;font-variant-numeric:tabular-nums}
.bar-track{height:12px;background:var(--mute-bg);border-radius:6px;overflow:hidden}
.bar{display:block;height:100%;border-radius:6px}.bar.muted{background:var(--muted)}.bar.accent{background:var(--accent)}
footer{color:var(--muted);font-size:.8rem;text-align:center;margin-top:24px}
@media (max-width:560px){main{padding:16px 16px 40px}section{padding:14px}h1{font-size:1.3rem}
.bar-row{grid-template-columns:92px 1fr 48px}}
"""

_JS = """
(function(){
var root=document.documentElement,key='hws-theme',btn=document.getElementById('theme-toggle');
function current(){var t=root.getAttribute('data-theme');if(t){return t;}
return window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
function apply(t){root.setAttribute('data-theme',t);btn.textContent=t==='dark'?'Light theme':'Dark theme';
btn.setAttribute('aria-pressed',t==='dark'?'true':'false');}
var h=(location.hash||'').replace('#','');
if(h==='theme=dark'||h==='theme=light'){root.setAttribute('data-theme',h.slice(6));}
else{try{var s=localStorage.getItem(key);if(s==='dark'||s==='light'){root.setAttribute('data-theme',s);}}catch(e){}}
apply(current());
btn.addEventListener('click',function(){var t=current()==='dark'?'light':'dark';apply(t);
try{localStorage.setItem(key,t);}catch(e){}});
})();
"""


def render_html(data: dict) -> str:
    """Render ``build()`` output as one offline HTML page."""
    data = data if isinstance(data, Mapping) else {}
    notes = [n for n in data.get("notes") or [] if isinstance(n, str)]
    notes_html = ("" if not notes else
                  '<section class="notes" aria-labelledby="notes-h"><h2 id="notes-h">Missing artifacts</h2><ul>'
                  + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul></section>")
    body = "".join([
        _tiles(data),
        notes_html,
        _section("doctor", "Install / doctor", _doctor_html(data.get("doctor"))),
        _section("acceptance", "Acceptance: stock vs current vs framework", _acceptance_html(data.get("acceptance"))),
        _section("evolve", "Evolve history", _evolve_html(data.get("evolve")),
                 "A proposal is kept only when R_val beats R_best on the validation split."),
        _section("effectiveness", "Held-out effectiveness", _effectiveness_html(data.get("effectiveness")),
                 "Test tasks never reach the maintainer, proposer or gate; same model and turn budget for both arms."),
        _section("costs", "Costs", _costs_html(data.get("costs"))),
    ])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f'<title>hermes-wikiskill report</title><style>{_CSS}</style></head><body><main>'
        '<header class="top"><div><h1>hermes-wikiskill report</h1>'
        f'<p class="sub">Workspace <code>{_esc(data.get("ws") or "?")}</code> &middot; generated '
        f'{_esc(data.get("generated") or "?")}</p></div>'
        '<button type="button" class="theme" id="theme-toggle" aria-pressed="false">Dark theme</button></header>'
        f'{body}<footer>Every number above is read from the workspace, eval, acceptance and doctor artifacts; '
        'the raw report data is embedded in this file as JSON (<code>#hws-data</code>).</footer></main>'
        f'{_json_island(data)}<script>{_JS}</script></body></html>'
    )
