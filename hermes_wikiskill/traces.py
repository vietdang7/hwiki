"""Raw Layer: immutable execution traces (upstream layout kept, atomic writes).

``raw/traces/iter-NN/<split>/<task>.meta.json`` + ``<task>.jsonl`` transcript copy.
A real trace is never replaced unless ``overwrite=True``; dry-runs never reach
this module (D2).
"""

from __future__ import annotations

import json
import os
import shutil

from .util import atomic_write_json


def trace_dir(ws: str, it: int, split: str) -> str:
    return os.path.join(ws, "raw", "traces", f"iter-{it:02d}", split)


def meta_path(ws: str, it: int, split: str, tid: str) -> str:
    return os.path.join(trace_dir(ws, it, split), f"{tid}.meta.json")


def transcript_path(ws: str, it: int, split: str, tid: str) -> str:
    return os.path.join(trace_dir(ws, it, split), f"{tid}.jsonl")


def exists(ws: str, it: int, split: str, tid: str) -> bool:
    return os.path.exists(meta_path(ws, it, split, tid))


def save_trace(ws: str, it: int, split: str, tid: str, meta: dict,
               transcript_src: str | None = None, overwrite: bool = False) -> dict:
    p = meta_path(ws, it, split, tid)
    if os.path.exists(p) and not overwrite:
        raise FileExistsError(f"trace already exists: {p} (pass --overwrite to replace it)")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tp = transcript_path(ws, it, split, tid)
    if transcript_src and os.path.exists(transcript_src):
        shutil.copyfile(transcript_src, tp + ".tmp")
        os.replace(tp + ".tmp", tp)
        meta = {**meta, "transcript": os.path.relpath(tp, ws)}
    elif os.path.exists(tp):
        os.remove(tp)  # never pair a new meta with a stale transcript
    atomic_write_json(p, meta)
    return meta


def load_trace(ws: str, it: int, split: str, tid: str) -> dict:
    with open(meta_path(ws, it, split, tid), encoding="utf-8") as f:
        return json.load(f)


def list_traces(ws: str, it: int | None = None, split: str | None = None) -> list[dict]:
    """Return [{"it", "split", "task_id", "path", "meta"}] (sorted)."""
    out = []
    root = os.path.join(ws, "raw", "traces")
    if not os.path.isdir(root):
        return out
    for d in sorted(os.listdir(root)):
        if not d.startswith("iter-"):
            continue
        try:
            n = int(d.split("-")[1])
        except ValueError:
            continue
        if it is not None and n != it:
            continue
        for s in sorted(os.listdir(os.path.join(root, d))):
            if split is not None and s != split:
                continue
            sdir = os.path.join(root, d, s)
            if not os.path.isdir(sdir):
                continue
            for f in sorted(os.listdir(sdir)):
                if not f.endswith(".meta.json"):
                    continue
                p = os.path.join(sdir, f)
                try:
                    with open(p, encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    continue
                out.append({"it": n, "split": s, "task_id": f[: -len(".meta.json")],
                            "path": p, "meta": meta})
    return out
