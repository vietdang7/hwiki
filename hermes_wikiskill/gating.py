"""Gating: validation runs, strict-improvement acceptance, skill-set git management
and the persisted evolution state (Algorithm 1: accept iff R_val > R_best, else roll
back skills only; wiki retained).

Fixes: D2 (dry-run never persists, real traces never silently overwritten), D7
(launch failures propagate, never graded), D11 (proposal names/paths contained,
symlinks refused), D15 (every git call checked; rollback verified against the
base commit), atomic state writes.
"""

from __future__ import annotations

import json
import os

from . import agents, gitutil, tasks as tasks_mod, traces
from .errors import GitError, UnsafePathError
from .skillfiles import normalize_skill_md
from .util import atomic_write_json, check_skill_name, now_iso, safe_join, safe_relpath

STATE_FILE = os.path.join("runs", "state.json")
PATCH_OPS = ("append", "replace", "insert_after")


def state_path(ws: str) -> str:
    return os.path.join(ws, STATE_FILE)


def load_state(ws: str) -> dict:
    p = state_path(ws)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"domain": os.path.basename(os.path.abspath(ws).rstrip("/")), "r_best": None,
            "baseline": None, "next_iter": 1, "history": []}


def save_state(ws: str, state: dict) -> None:
    atomic_write_json(state_path(ws), state)


# ---------------------------------------------------------------- git (skills)

def active_dir(ws: str) -> str:
    return os.path.join(ws, "skills", "active")


def ensure_active_repo(ws: str) -> None:
    d = active_dir(ws)
    os.makedirs(d, exist_ok=True)
    if not os.path.isdir(os.path.join(d, ".git")):
        gitutil.init_repo(d, "S0: empty skill set")


def git_active(ws: str, *args: str, check: bool = True):
    return gitutil.git(active_dir(ws), *args, check=check)


def active_head(ws: str) -> str:
    return gitutil.head(active_dir(ws))


def active_dirty(ws: str) -> list[str]:
    """R1-02: working-tree changes in skills/active vs HEAD (tracked, untracked, ignored).
    Only the harness (apply_proposal / accept) may change it — a role never."""
    out = git_active(ws, "status", "--porcelain", "--untracked-files=all", "--ignored").stdout
    return [line[3:] for line in out.splitlines() if line.strip()] + gitutil.nested_git(active_dir(ws))


def reset_active(ws: str) -> None:
    """Drop every working-tree change in skills/active: back to HEAD (the last accepted or
    base commit, both harness-made)."""
    git_active(ws, "reset", "-q", "--hard", "HEAD")
    gitutil.remove_nested_git(active_dir(ws))  # T11 m1
    git_active(ws, "clean", "-q", "-ffdx")


def ungated_reason(ws: str) -> str | None:
    """R2-05: why skills/active's working tree is not the gated skill set (None = it is)."""
    rr = load_state(ws).get("role_running")
    dirty = active_dirty(ws) if os.path.isdir(os.path.join(active_dir(ws), ".git")) else []
    if rr:
        return f"a {rr.get('role') if isinstance(rr, dict) else 'role'} run was interrupted (state.role_running)"
    if dirty:
        return f"skills/active has ungated changes ({', '.join(dirty[:3])})"
    return None


def guard_gated_tree(ws: str, verb: str) -> None:
    """R2-05: ``gate`` runs rollouts against the skills/active working tree — refuse while it is
    not the gated skill set (interrupted role run, or tracked/untracked/ignored changes)."""
    from .errors import HwsError

    why = ungated_reason(ws)
    if why:
        raise HwsError(f"{verb} refused on {ws}: {why}. Resume first with `hermes-wikiskill evolve {ws} --iters <N>` "
                       f"(it restores the last gated skill set), then re-run {verb}.")


def commit_base(ws: str, k: int) -> str:
    """Snapshot the active skill set before a proposal is applied. Returns the base commit.

    R1-02: refuses a dirty tree — a role's direct writes must never become the "base"."""
    dirty = active_dirty(ws)
    if dirty:
        raise ValueError(f"skills/active has uncommitted changes {dirty[:5]} — role wrote skills/active directly")
    git_active(ws, "add", "-A")
    git_active(ws, "commit", "-q", "--allow-empty", "-m", f"base iter-{k}")
    return active_head(ws)


def validate_proposal(proposal: object) -> dict:
    """Shape + containment checks for an LLM-written proposal (D11). Raises ValueError."""
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object")
    action = proposal.get("action")
    if action == "no_action":
        return proposal
    if action not in ("create", "patch"):
        raise ValueError(f"unknown proposal action {action!r}")
    check_skill_name(proposal.get("name"))
    if action == "create":
        if not isinstance(proposal.get("skill_md"), str) or not proposal["skill_md"].strip():
            raise ValueError("create proposal needs a non-empty skill_md")
        if proposal.get("purpose_md") is not None and not isinstance(proposal["purpose_md"], str):
            raise ValueError("purpose_md must be a string")
    else:
        rel = safe_relpath(proposal.get("file", "SKILL.md"))
        if rel.split("/")[0] == ".git":
            raise UnsafePathError(f"patch file inside .git: {rel!r}")
        edits = proposal.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("patch proposal needs a non-empty edits list")
        for e in edits:
            if not isinstance(e, dict) or e.get("op") not in PATCH_OPS:
                raise ValueError(f"bad patch edit {e!r}")
            if not isinstance(e.get("content"), str):
                raise ValueError("patch edit content must be a string")
            if e["op"] != "append" and not isinstance(e.get("target"), str):
                raise ValueError(f"patch {e['op']} needs a string target")
    return proposal


def _write_contained(root: str, rel: str, text: str) -> None:
    path = safe_join(root, rel)
    if os.path.islink(path):
        raise UnsafePathError(f"refusing to write through a symlink: {rel!r}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def apply_proposal(ws: str, proposal: dict) -> str:
    """Apply a validated proposal to skills/active. Returns a description."""
    validate_proposal(proposal)
    action = proposal["action"]
    if action == "no_action":
        return "no_action"
    name = proposal["name"]
    root = active_dir(ws)
    dest = safe_join(root, name)
    if os.path.islink(dest) or (os.path.exists(dest) and not os.path.isdir(dest)):
        raise UnsafePathError(f"skill path is not a plain directory: {name!r}")
    if action == "create":
        os.makedirs(dest, exist_ok=True)
        _write_contained(dest, "SKILL.md", normalize_skill_md(proposal["skill_md"], name))
        if proposal.get("purpose_md"):
            _write_contained(dest, "PURPOSE.md", proposal["purpose_md"])
        return f"create {name}"
    rel = safe_relpath(proposal.get("file", "SKILL.md"))
    target = safe_join(dest, rel)
    if not os.path.isfile(target) or os.path.islink(target):
        raise ValueError(f"patch target does not exist: {name}/{rel}")
    with open(target, encoding="utf-8") as f:
        content = f.read()
    for edit in proposal["edits"]:
        op = edit["op"]
        if op == "append":
            content += "\n" + edit["content"]
        elif edit["target"] not in content:
            raise ValueError(f"patch {op} target not found in {name}: {edit['target'][:60]!r}")
        elif op == "replace":
            content = content.replace(edit["target"], edit["content"], 1)
        else:
            content = content.replace(edit["target"], edit["target"] + "\n" + edit["content"], 1)
    if rel == "SKILL.md":
        content = normalize_skill_md(content, name)
    _write_contained(dest, rel, content)
    return f"patch {name}"


def rollback(ws: str, base: str | None = None) -> None:
    """Restore skills/active EXACTLY to ``base`` (default HEAD) and verify it (D15)."""
    target = base or active_head(ws)
    git_active(ws, "reset", "--hard", "-q", target)
    gitutil.remove_nested_git(active_dir(ws))  # T11 m1
    git_active(ws, "clean", "-ffdx", "-q")
    if active_head(ws) != target or not gitutil.is_clean(active_dir(ws)):
        raise GitError(f"rollback of {active_dir(ws)} to {target[:12]} could not be verified")


def skill_diff(ws: str) -> str:
    """Diff of the candidate vs the base, new files included (upstream missed them)."""
    git_active(ws, "add", "-A")
    return git_active(ws, "diff", "--cached").stdout


def accept_commit(ws: str, k: int, r_val: float) -> str:
    git_active(ws, "add", "-A")
    git_active(ws, "commit", "-q", "--allow-empty", "-m", f"accept iter-{k} R={r_val}")
    if not gitutil.is_clean(active_dir(ws)):
        raise GitError(f"accept commit left {active_dir(ws)} dirty")
    return active_head(ws)


# ---------------------------------------------------------------- run helpers

def task_tag(it: int, task: dict) -> str:
    return f"iter-{it:02d}/{task['split']}/{task['id']}"


def run_task(ws: str, task: dict, it: int, *, model: str | None = None,
             runner=agents.run_agent, dry_run: bool = False, overwrite: bool = False,
             runner_kwargs: dict | None = None) -> dict:
    """Isolated inference rollout on one task + grading + trace capture (D19).

    Dry-run: returns the planned command, writes nothing (D2/D3).
    Hermes launch failures / invisible skills raise (D7/D1) — no score, no trace.
    """
    from . import rollout
    from .backends import hermes

    tag = task_tag(it, task)
    run_dir = os.path.join(ws, "runs", tag)
    skill_dirs = hermes.active_skill_dirs(ws)
    if dry_run:
        r = rollout.run_task_rollout(ws, task, run_dir=run_dir, skill_dirs=skill_dirs, model=model,
                                     runner=runner, dry_run=True, runner_kwargs=runner_kwargs, tag=tag)
        return {**task, "score": None, "result": r["result"], "dry_run": True}
    if traces.exists(ws, it, task["split"], task["id"]) and not overwrite:
        raise FileExistsError(f"trace already exists: {traces.meta_path(ws, it, task['split'], task['id'])} "
                              f"(pass --overwrite to replace it)")
    r = rollout.run_task_rollout(ws, task, run_dir=run_dir, skill_dirs=skill_dirs, model=model,
                                 runner=runner, runner_kwargs=runner_kwargs, tag=tag)
    res = r["result"]
    meta = {
        "task_id": task["id"], "split": task["split"], "iter": it, "title": task.get("title", ""),
        "score": r["score"], "model": model, "exit_code": res.get("exit_code"),
        "duration_s": res.get("duration_s"), "ts": now_iso(),
        "task_sha256": tasks_mod.task_sha256(task),
        "skills_expected": res.get("skills_expected", []),
        "skills_visible": res.get("skills_visible", []),
        "skill_views": res.get("skill_views", []), "usage": res.get("usage", {}),
        "hermes_version": res.get("hermes_version"), "session_id": res.get("session_id"),
        "isolated": True, "sandbox_escape": r["escapes"],
    }
    out = {**task, "score": r["score"], "result": res, "escapes": r["escapes"]}
    if "feedback" in r:  # D18: grader feedback for TRAIN only, never val/test
        meta["feedback"] = out["feedback"] = r["feedback"]
    traces.save_trace(ws, it, task["split"], task["id"], meta,
                      transcript_src=res.get("session_file"), overwrite=overwrite)
    return out


def mean_score(results: list[dict]) -> float:
    scores = [r["score"] for r in results if r.get("score") is not None]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


def run_gate(ws: str, tasks: list[dict], it: int, *, model: str | None = None,
             runner=agents.run_agent, dry_run: bool = False, overwrite: bool = False,
             runner_kwargs: dict | None = None) -> dict:
    """Validation rollout over a split with the *current* active skills.

    Any rollout error aborts the whole gate (no partial mean → no decision)."""
    results = [run_task(ws, t, it, model=model, runner=runner, dry_run=dry_run,
                        overwrite=overwrite, runner_kwargs=runner_kwargs) for t in tasks]
    return {"iter": it, "results": results, "mean": None if dry_run else mean_score(results),
            "escapes": sum(len(r.get("escapes") or []) for r in results)}
