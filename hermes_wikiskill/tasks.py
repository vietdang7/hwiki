"""Task registry: load/validate tasks, splits, contained sandbox materialization.

Upstream schema kept (id, split, title, prompt, sandbox{rel: content}, grader{…}).
Changes: sandbox paths must stay inside the sandbox (D10); a workspace's
``tasks.json`` holds only train/val, ``test`` is accepted only by ``eval --tasks``
files (D8); replacing tasks re-materializes every sandbox (D13).
"""

from __future__ import annotations

import json
import os
import re
import shutil

from .util import atomic_write_json, canonical_sha256, safe_join, safe_relpath, sha256_file

KNOWN_GRADERS = {"exact", "contains", "json_field", "code_stdout"}
SEALABLE = {"exact", "json_field", "code_stdout"}
_HEX64 = re.compile(r"[0-9a-f]{64}")
TASKS_FILE = "tasks.json"
SANDBOX_ROOT = os.path.join("bench", "tasks")
WORKSPACE_SPLITS = ("train", "val")
ALL_SPLITS = ("train", "val", "test")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")


def tasks_path(ws: str) -> str:
    return os.path.join(ws, TASKS_FILE)


def re_slug(s: str) -> bool:
    return bool(isinstance(s, str) and _SLUG.fullmatch(s))


def validate(tasks: list[dict], *, allowed_splits: tuple[str, ...] = WORKSPACE_SPLITS) -> None:
    if not isinstance(tasks, list):
        raise ValueError("tasks file must hold a JSON list of task objects")
    ids = set()
    for t in tasks:
        if not isinstance(t, dict):
            raise ValueError(f"task must be an object: {t!r}")
        if not re_slug(t.get("id")):
            raise ValueError(f"task id must be a slug: {t.get('id')!r}")
        if t["id"] in ids:
            raise ValueError(f"duplicate task id: {t['id']}")
        ids.add(t["id"])
        if t.get("split") not in allowed_splits:
            raise ValueError(f"task {t['id']}: split must be {'|'.join(allowed_splits)}"
                             + (" (held-out test tasks belong in a separate file passed to "
                                "`eval --tasks`, never in a workspace)" if t.get("split") == "test" else ""))
        if not t.get("prompt"):
            raise ValueError(f"task {t['id']}: missing prompt")
        g = t.get("grader") or {}
        sandbox = t.get("sandbox") or {}
        if not isinstance(sandbox, dict):
            raise ValueError(f"task {t['id']}: sandbox must be an object")
        if not sandbox and g.get("type") != "code_stdout":
            raise ValueError(f"task {t['id']}: empty sandbox")
        if g.get("type") not in KNOWN_GRADERS:
            raise ValueError(f"task {t['id']}: unknown grader {g.get('type')!r}")
        for rel in sandbox:
            safe_relpath(rel)  # D10: raises UnsafePathError on absolute / ../ paths
        for key in ("file", "script"):
            if key in g:
                safe_relpath(g[key])
        if g.get("type") in SEALABLE:
            if "expected_sha256" in g:
                if "expected" in g:
                    raise ValueError(f"task {t['id']}: sealed grader must not also carry `expected`")
                if not _HEX64.fullmatch(str(g["expected_sha256"])):
                    raise ValueError(f"task {t['id']}: expected_sha256 must be 64 lowercase hex chars")
            elif "expected" not in g:
                raise ValueError(f"task {t['id']}: grader needs `expected` or `expected_sha256`")


def load_file(path: str, *, allowed_splits: tuple[str, ...] = ALL_SPLITS) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        tasks = json.load(f)
    validate(tasks, allowed_splits=allowed_splits)
    return tasks


def load(ws: str) -> list[dict]:
    p = tasks_path(ws)
    if not os.path.exists(p):
        raise FileNotFoundError(f"no {TASKS_FILE} in workspace {ws!r} (run `hermes-wikiskill init`)")
    return load_file(p, allowed_splits=WORKSPACE_SPLITS)


def save(ws: str, tasks: list[dict]) -> None:
    validate(tasks, allowed_splits=WORKSPACE_SPLITS)
    atomic_write_json(tasks_path(ws), tasks)


def splits(ws: str) -> dict[str, list[dict]]:
    """Train/val only — the evolution loop can never see held-out tasks (D8)."""
    tasks = load(ws)
    return {s: [t for t in tasks if t["split"] == s] for s in WORKSPACE_SPLITS}


def task_sha256(task: dict) -> str:
    return canonical_sha256(task)


def tasks_file_sha256(path: str) -> str:
    return sha256_file(path)


def sandbox_dir(ws: str, task_id: str) -> str:
    return os.path.join(ws, SANDBOX_ROOT, task_id)


def materialize_at(d: str, task: dict, force: bool = False) -> str:
    """Write the task's sandbox files into ``d`` (contained, D10)."""
    os.makedirs(d, exist_ok=True)
    wanted = {safe_relpath(rel).split("/")[0] for rel in task.get("sandbox", {})}
    if force:
        for name in os.listdir(d):
            if name in wanted:
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
    for rel, content in task.get("sandbox", {}).items():
        p = safe_join(d, rel)
        if force and os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        if force or not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if os.path.islink(p):
                os.unlink(p)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
    return d


def materialize(ws: str, task: dict, force: bool = False) -> str:
    return materialize_at(sandbox_dir(ws, task["id"]), task, force=force)


def materialize_all(ws: str, tasks: list[dict], force: bool = False) -> None:
    for t in tasks:
        materialize(ws, t, force=force)


def seal(task: dict) -> dict:
    """Replace a plaintext answer key by its sha256 (D19). Unsealable graders are returned as-is."""
    from .scoring import seal_digest

    g = task.get("grader") or {}
    if g.get("type") not in SEALABLE or "expected" not in g:
        return task
    sealed = {k: v for k, v in g.items() if k != "expected"}
    sealed["expected_sha256"] = seal_digest(g["expected"])
    return {**task, "grader": sealed}


def seal_split(tasks: list[dict], splits_to_seal: tuple[str, ...] = ("val", "test")) -> list[dict]:
    """Seal every task of the given splits; TRAIN keeps plaintext (grader feedback needs it, D18)."""
    return [seal(t) if t.get("split") in splits_to_seal else t for t in tasks]


def replace_tasks(ws: str, tasks: list[dict]) -> dict:
    """Install a (new) task set: seal VAL (D19) + save + force re-materialize + drop removed sandboxes (D13)."""
    tasks = seal_split(tasks)
    save(ws, tasks)
    root = os.path.join(ws, SANDBOX_ROOT)
    keep = {t["id"] for t in tasks}
    removed = []
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            if name not in keep:
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                removed.append(name)
    materialize_all(ws, tasks, force=True)
    unsealed = [t["id"] for t in tasks if t["split"] == "val" and "expected_sha256" not in t["grader"]]
    return {"tasks": len(tasks), "removed_sandboxes": removed, "unsealed_val": unsealed}
