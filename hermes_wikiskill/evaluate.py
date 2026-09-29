"""Held-out evaluation (D8): run a tasks file (any split, typically the bench's
``test.json``; sealed graders accepted, D19) with the active or the empty skill set.

Isolation: each rollout runs outside the workspace (``rollout.run_task_rollout``, D19);
results are written under ``<ws>/eval/<tag>/`` with a frozen COPY of the active skills.
``skills/active``, traces, state and wiki are never touched, and nothing under
``eval/`` is ever referenced by maintainer/proposer prompts.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile

from . import agents, credentials, gating, gitutil, rollout, tasks as tasks_mod
from .backends import hermes
from .errors import HwsError, UsageError
from .skillfiles import skill_name
from .util import atomic_write_json, now_iso, stamp

_TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")


def eval_dir(ws: str, tag: str) -> str:
    return os.path.join(ws, "eval", tag)


def _model(ws: str) -> str | None:
    try:
        return hermes.profile_plan(ws)["config"]["model"].get("default")
    except (KeyError, AttributeError):
        return None


def gated_commit(ws: str) -> str:
    """T10c: the last GATED skills/active commit. A hard-killed role may have moved HEAD
    (`git commit` in skills/active); state.role_running.active_head is the commit it started from."""
    active_dir = gating.active_dir(ws)
    rr = gating.load_state(ws).get("role_running")
    ah = rr.get("active_head") if isinstance(rr, dict) else None
    return ah if gitutil.commit_exists(active_dir, ah) else gitutil.head(active_dir)


def evaluate(ws: str, tasks_file: str | None = None, *, tasks: list[dict] | None = None,
             skills: str = "active", repeat: int = 1, tag: str | None = None,
             max_turns: int | None = 15, run_budget: int | None = 300, model: str | None = None,
             dry_run: bool = False, overwrite: bool = False, runner=agents.run_agent,
             verbose: bool = False) -> dict:
    """R2-05: ``skills="active"`` measures the gated skill set = skills/active HEAD, exported to a
    private hws- temp dir (removed afterwards) — never the working tree, which a hard-killed role run
    may have left dirty (a warning is printed and recorded instead)."""
    head_dir, gated = None, None
    active_dir = gating.active_dir(ws)
    if skills == "active" and os.path.isdir(os.path.join(active_dir, ".git")):
        bad = gitutil.unsafe_metadata(active_dir)  # T10c: info/attributes, grafts, replace refs alter the export
        if bad:
            raise HwsError(f"eval refused on {ws}: skills/active git metadata was modified outside the harness "
                           f"({', '.join(bad[:5])}) — evolve refuses this workspace too; inspect and restore "
                           f"skills/active/.git before measuring it")
        gated = gated_commit(ws)
        head_dir = tempfile.mkdtemp(prefix="hws-evalhead-")
    try:
        if head_dir:
            gitutil.export_tree(active_dir, gated, head_dir)
        return _evaluate(ws, tasks_file, tasks=tasks, skills=skills, repeat=repeat, tag=tag,
                         max_turns=max_turns, run_budget=run_budget, model=model, dry_run=dry_run,
                         overwrite=overwrite, runner=runner, verbose=verbose, head_dir=head_dir,
                         gated=gated)
    finally:
        if head_dir:
            shutil.rmtree(head_dir, ignore_errors=True)


def _evaluate(ws: str, tasks_file: str | None = None, *, tasks: list[dict] | None = None,
             skills: str = "active", repeat: int = 1, tag: str | None = None,
             max_turns: int | None = 15, run_budget: int | None = 300, model: str | None = None,
             dry_run: bool = False, overwrite: bool = False, runner=agents.run_agent,
             verbose: bool = False, head_dir: str | None = None, gated: str | None = None) -> dict:
    if skills not in ("active", "empty"):
        raise UsageError("--skills must be active|empty")
    if repeat < 1:
        raise UsageError("--repeat must be >= 1")
    if tasks is None:
        if not tasks_file:
            raise UsageError("eval needs --tasks FILE")
        tasks = tasks_mod.load_file(tasks_file)
        tasks_sha = tasks_mod.tasks_file_sha256(tasks_file)
    else:
        tasks_mod.validate(tasks, allowed_splits=tasks_mod.ALL_SPLITS)
        from .util import canonical_sha256
        tasks_sha = canonical_sha256(tasks)
    tag = tag or f"{skills}-{stamp()}"
    if not _TAG.fullmatch(tag):
        raise UsageError(f"invalid eval tag {tag!r}")
    edir = eval_dir(ws, tag)
    active_dir = gating.active_dir(ws)
    head_now = gitutil.head(active_dir) if os.path.isdir(os.path.join(active_dir, ".git")) else None
    commit = gated or head_now
    if skills != "active":
        src_dirs = []
    elif head_dir:  # R2-05: the HEAD export, same selection rule as hermes.active_skill_dirs
        src_dirs = [os.path.join(head_dir, n) for n in sorted(os.listdir(head_dir))
                    if not n.startswith(".") and os.path.isfile(os.path.join(head_dir, n, "SKILL.md"))]
    else:
        src_dirs = hermes.active_skill_dirs(ws)
    names = [skill_name(d) for d in src_dirs]
    head = {"tag": tag, "ws": os.path.abspath(ws), "skills": skills, "skill_names": names,
            "active_commit": commit, "model": model or _model(ws), "max_turns": max_turns,
            # QA-F8c: basename only — an absolute held-out path in the ws leads a role to it
            "tasks_file": os.path.basename(tasks_file) if tasks_file else None,
            "tasks_sha256": tasks_sha, "n_tasks": len(tasks), "repeat_count": repeat}
    if dry_run:
        return {**head, "dry_run": True, "eval_dir": edir, "rollouts": repeat * len(tasks)}
    ungated = gating.ungated_reason(ws) if skills == "active" else None
    if ungated and head_now and commit != head_now:
        ungated += f"; skills/active HEAD moved to {head_now[:12]} (not gated)"
    if ungated:
        print(f"[hermes-wikiskill] warning: {ungated} — evaluating the gated skills of commit "
              f"{(commit or '')[:12]}, not the working tree", file=sys.stderr, flush=True)
    if os.path.exists(edir):
        if not overwrite:
            raise UsageError(f"eval tag already exists: {edir} (choose another --tag or --overwrite)")
        shutil.rmtree(edir)
    os.makedirs(edir)
    snap_dirs = []
    for d in src_dirs:  # frozen copy: the eval can never modify skills/active
        dst = os.path.join(edir, "skills", os.path.basename(d))
        hermes.copy_skill_dir(d, dst)  # R1-16: symlinks=True, a skill with links is refused
        snap_dirs.append(dst)
    started = now_iso()
    hermes_version = None
    repeats = []
    for rep in range(1, repeat + 1):
        results = []
        for t in tasks:
            run_dir = os.path.join(edir, "runs", f"rep-{rep}", t["id"])
            r = rollout.run_task_rollout(ws, t, run_dir=run_dir, skill_dirs=snap_dirs, model=model,
                                         runner=runner, tag=f"eval-{tag}",
                                         runner_kwargs={"max_turns": max_turns, "run_budget": run_budget})
            res = r["result"]
            hermes_version = hermes_version or res.get("hermes_version")
            results.append({"task_id": t["id"], "score": r["score"], "duration_s": res.get("duration_s"),
                            "exit_code": res.get("exit_code"), "usage": res.get("usage", {}),
                            "skill_views": res.get("skill_views", []),
                            "skills_visible": res.get("skills_visible", []),
                            "skills_listed": res.get("skills_listed", []),
                            "sandbox_escape": r["escapes"],
                            "session": os.path.relpath(res["session_file"], ws)
                            if res.get("session_file") else None})
            if verbose:
                print(f"[hermes-wikiskill] eval {tag} rep {rep} {t['id']}: {r['score']}"
                      + (f" ESCAPE {r['escapes'][:1]}" if r["escapes"] else ""), flush=True)
        repeats.append({"rep": rep, "mean": gating.mean_score(results), "results": results,
                        "escapes": sum(1 for x in results if x["sandbox_escape"])})
    means = [r["mean"] for r in repeats]
    if ungated:
        head = {**head, "ungated_tree_ignored": ungated}
    summary = {**head, "hermes_version": hermes_version, "repeats": repeats,
               "escapes": sum(r["escapes"] for r in repeats), "isolated": True,
               "mean": round(sum(means) / len(means), 4), "started": started, "finished": now_iso()}
    atomic_write_json(os.path.join(edir, "summary.json"), summary)
    return summary


__all__ = ["evaluate", "eval_dir", "gated_commit", "credentials"]
