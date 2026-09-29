"""Paired comparison of two workspaces (upstream compare.py statistics).

Fixes D16: pairing requires IDENTICAL val task definitions (sha256 per task), not
just equal ids; runs go through ``evaluate`` under unique ``eval/compare-<ts>-A|B``
tags, so no trace or earlier comparison is overwritten.
"""

from __future__ import annotations

import math

from . import tasks as tasks_mod
from .evaluate import evaluate
from .util import stamp


def _two_sided_binomial_p(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    obs = abs(b - c)
    return min(sum(math.comb(n, k) * 0.5 ** n for k in range(n + 1) if abs(2 * k - n) >= obs), 1.0)


def paired_val_tasks(ws_a: str, ws_b: str) -> list[dict]:
    va = {t["id"]: t for t in tasks_mod.load(ws_a) if t["split"] == "val"}
    vb = {t["id"]: t for t in tasks_mod.load(ws_b) if t["split"] == "val"}
    if set(va) != set(vb):
        raise ValueError(f"workspaces have different val task sets (A {len(va)}, B {len(vb)})")
    if not va:
        raise ValueError("no val tasks in either workspace; nothing to compare")
    changed = sorted(i for i in va if tasks_mod.task_sha256(va[i]) != tasks_mod.task_sha256(vb[i]))
    if changed:
        raise ValueError(f"val tasks share ids but differ in content (prompt/sandbox/grader): {changed}")
    return [va[i] for i in sorted(va)]


def run_comparison(ws_a: str, ws_b: str, iters: int = 3, *, dry_run: bool = False,
                   max_turns: int | None = 15, tag: str | None = None, **kw) -> dict:
    tasks = paired_val_tasks(ws_a, ws_b)
    tag = tag or f"compare-{stamp()}"
    runs = {}
    for side, ws in (("A", ws_a), ("B", ws_b)):
        runs[side] = evaluate(ws, tasks=tasks, skills="active", repeat=iters, tag=f"{tag}-{side}",
                              max_turns=max_turns, dry_run=dry_run, **kw)
    if dry_run:
        return {"dry_run": True, "tag": tag, "tasks": [t["id"] for t in tasks], "iters": iters,
                "plans": runs}
    scores = {s: {t["id"]: [] for t in tasks} for s in "AB"}
    for s in "AB":
        for rep in runs[s]["repeats"]:
            for r in rep["results"]:
                scores[s][r["task_id"]].append(1.0 if (r["score"] or 0) >= 0.5 else 0.0)
    rows, da, db = [], 0, 0
    for t in tasks:
        a, b = scores["A"][t["id"]], scores["B"][t["id"]]
        for x, y in zip(a, b):
            da += x == 1 and y == 0
            db += x == 0 and y == 1
        rows.append({"task": t["id"], "A": round(sum(a) / iters, 3), "B": round(sum(b) / iters, 3),
                     "outcome": "A" if sum(a) > sum(b) else "B" if sum(b) > sum(a) else "tie"})
    pa = sum(r["A"] for r in rows) / len(rows)
    pb = sum(r["B"] for r in rows) / len(rows)
    p = _two_sided_binomial_p(da, db)
    verdict = ("A better" if pa > pb and p <= 0.05 else
               "B better" if pb > pa and p <= 0.05 else "no significant difference")
    return {"tag": tag, "iters": iters, "tasks": rows,
            "pass_rate": {"A": round(pa, 4), "B": round(pb, 4)},
            "discordant_pairs": {"A_pass_B_fail": int(da), "B_pass_A_fail": int(db)},
            "p_value": round(p, 4), "verdict": verdict,
            "eval_tags": {"A": runs["A"]["tag"], "B": runs["B"]["tag"]}}


def format_report(r: dict) -> str:
    if r.get("dry_run"):
        return f"compare (dry-run): {len(r['tasks'])} val tasks x {r['iters']} runs per workspace"
    lines = [f"compare: {r['iters']} run(s) per workspace, val-only", "",
             f"{'task':<22s} {'A':>6s} {'B':>6s}  winner", "-" * 42]
    lines += [f"{x['task']:<22s} {x['A']:>6.3f} {x['B']:>6.3f}  {x['outcome']}" for x in r["tasks"]]
    lines += ["-" * 42, f"pass rate: A={r['pass_rate']['A']:.3f}  B={r['pass_rate']['B']:.3f}",
              f"paired exact binomial p={r['p_value']:.4f}", f"verdict: {r['verdict']}"]
    return "\n".join(lines)
