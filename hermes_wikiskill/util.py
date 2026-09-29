"""Small shared helpers: atomic writes, hashing, path containment, time."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from .errors import UnsafePathError

SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp() -> str:
    """Filesystem-safe UTC timestamp, e.g. 20260928T193000Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def atomic_write_text(path: str, text: str, *, mode: int | None = None) -> None:
    """Write via a temp file in the same directory + os.replace (never half-written)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hws-tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: str, data: object, *, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, default=str) + "\n", mode=mode)


def read_json(path: str, default: object = None) -> object:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj: object) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())


def tree_digest(root: str, exclude: tuple[str, ...] = (".git",)) -> str:
    """Content digest of a directory tree (relative paths, file bytes, symlink targets)."""
    h = hashlib.sha256()
    if not os.path.isdir(root):
        return h.hexdigest()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude)
        rel_dir = os.path.relpath(dirpath, root)
        h.update(f"D {rel_dir}\n".encode())
        for name in sorted(filenames):
            if name in exclude:
                continue
            p = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if os.path.islink(p):
                h.update(f"L {rel} -> {os.readlink(p)}\n".encode())
            else:
                h.update(f"F {rel} {sha256_file(p)}\n".encode())
    return h.hexdigest()


def safe_relpath(rel: str) -> str:
    """Normalize a relative path that must stay inside its root (D10/D11)."""
    if not isinstance(rel, str) or not rel.strip():
        raise UnsafePathError(f"empty path: {rel!r}")
    if "\x00" in rel or "\\" in rel:
        raise UnsafePathError(f"invalid characters in path: {rel!r}")
    if os.path.isabs(rel) or rel.startswith("~"):
        raise UnsafePathError(f"absolute path not allowed: {rel!r}")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise UnsafePathError(f"path escapes its root: {rel!r}")
    return "/".join(parts)


def safe_join(root: str, rel: str) -> str:
    """Join ``rel`` under ``root``; refuse anything (incl. via symlinks) that escapes."""
    rel = safe_relpath(rel)
    path = os.path.join(root, *rel.split("/"))
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise UnsafePathError(f"path escapes {root!r} (via symlink?): {rel!r}")
    return path


def check_skill_name(name: object) -> str:
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        raise UnsafePathError(f"invalid skill name {name!r} (want [A-Za-z0-9][A-Za-z0-9_-]{{0,63}})")
    return name


def truncate(text: object, limit: int) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: max(0, limit - 15)] + "…[truncated]"
