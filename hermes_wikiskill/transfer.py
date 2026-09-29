"""Copy one workspace's active skills into another's active set (upstream transfer.py,
checked git: the destination's history lets a rejected transfer roll back)."""

from __future__ import annotations

import os
import shutil
import tempfile

from . import gating, gitutil
from .backends import hermes
from .errors import HwsError
from .util import check_skill_name


def active_skills(ws: str) -> list[str]:
    d = gating.active_dir(ws)
    if not os.path.isdir(d):
        return []
    return sorted(n for n in os.listdir(d)
                  if os.path.isdir(os.path.join(d, n)) and not n.startswith("."))


def transfer_skills(src: str, dst: str, *, force: bool = False, dry_run: bool = False) -> dict:
    """T11 m3: the source's GATED skill set (``evaluate.gated_commit``, exported like eval/compare) is
    copied — never its working tree, which a hard-killed role may have left with ungated skills."""
    active = gating.active_dir(src)
    if not os.path.isdir(os.path.join(active, ".git")):
        return _transfer(src, active, dst, force=force, dry_run=dry_run)
    from .evaluate import gated_commit
    bad = gitutil.unsafe_metadata(active)
    if bad:
        raise HwsError(f"transfer refused: {src} skills/active git metadata was modified outside the harness "
                       f"({', '.join(bad[:5])}) — inspect and restore skills/active/.git first")
    tmp = tempfile.mkdtemp(prefix="hws-transfer-")
    try:
        gitutil.export_tree(active, gated_commit(src), tmp)
        return _transfer(src, tmp, dst, force=force, dry_run=dry_run)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _transfer(src: str, src_dir: str, dst: str, *, force: bool, dry_run: bool) -> dict:
    dst_dir = gating.active_dir(dst)
    names = sorted(n for n in os.listdir(src_dir) if os.path.isdir(os.path.join(src_dir, n))
                   and not n.startswith(".")) if os.path.isdir(src_dir) else []
    if not names:
        raise ValueError(f"no active skills in source workspace: {src}")
    if not os.path.isdir(dst_dir):
        raise ValueError(f"no active skills dir in destination workspace: {dst}")
    transferred, skipped = [], []
    for n in names:
        check_skill_name(n)
        s, t = os.path.join(src_dir, n), os.path.join(dst_dir, n)
        if os.path.islink(s) or not os.path.isfile(os.path.join(s, "SKILL.md")) or (
                os.path.exists(t) and not force) or hermes.skill_symlinks(s):  # R1-16: links refused
            skipped.append(n)
            continue
        transferred.append(n)
        if dry_run:
            continue
        if os.path.exists(t):
            shutil.rmtree(t)
        hermes.copy_skill_dir(s, t)
    if transferred and not dry_run:
        gating.ensure_active_repo(dst)
        gitutil.git(dst_dir, "add", "-A")
        gitutil.git(dst_dir, "commit", "-q", "-m",
                    f"transfer from {os.path.basename(src)}: {', '.join(transferred)}")
    return {"transferred": transferred, "skipped": skipped, "dry_run": dry_run,
            "source": os.path.basename(os.path.abspath(src)),
            "destination": os.path.basename(os.path.abspath(dst))}


def format_report(m: dict) -> str:
    lines = [f"transfer {m['source']} → {m['destination']}" + (" (dry-run)" if m.get("dry_run") else "")]
    if m["transferred"]:
        lines.append("  installed: " + ", ".join(m["transferred"]))
    if m["skipped"]:
        lines.append("  skipped:   " + ", ".join(m["skipped"]) + " (no SKILL.md or present; --force)")
    if not m["transferred"]:
        lines.append("  nothing to transfer")
    return "\n".join(lines)
