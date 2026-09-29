"""Workspace lock (D14) and the optional per-rollout lock (HWS_ROLLOUT_LOCK).

Every mutating verb holds ``<ws>/.hws.lock`` (fcntl.flock) for its whole run. A
second process fails fast with exit 75 and the holder pid, or blocks with
``--wait``. Dry-runs never take the lock: they must not write anything.
"""

from __future__ import annotations

import contextlib
import fcntl
import os

from .errors import WorkspaceBusyError

LOCK_NAME = ".hws.lock"


def lock_path(ws: str) -> str:
    return os.path.join(ws, LOCK_NAME)


def holder_pid(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def workspace_lock(ws: str, *, wait: bool = False):
    os.makedirs(ws, exist_ok=True)
    path = lock_path(ws)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise WorkspaceBusyError(
                f"workspace busy: {ws} is locked by pid {holder_pid(path) or '?'} "
                f"(another evolve/run-task?); retry later or pass --wait") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield path
    finally:
        os.close(fd)


def is_locked(ws: str) -> bool:
    """True when some process holds the workspace lock (read-only probe)."""
    path = lock_path(ws)
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


@contextlib.contextmanager
def rollout_lock():
    """Blocking flock on $HWS_ROLLOUT_LOCK around ONE Hermes rollout (host-wide serialization)."""
    path = os.environ.get("HWS_ROLLOUT_LOCK")
    if not path:
        yield None
        return
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)
