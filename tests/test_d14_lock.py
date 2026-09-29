"""D14: one mutating process per workspace (exit 75 + holder pid), --wait, rollout lock."""

import fcntl
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import fw_ws, hws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import locking
from hermes_wikiskill.errors import WorkspaceBusyError


def _hold(path: str, seconds: float = 30) -> subprocess.Popen:
    code = textwrap.dedent(f"""
        import fcntl, os, sys, time
        fd = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0); os.write(fd, str(os.getpid()).encode())
        print("locked", flush=True)
        time.sleep({seconds})
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


@impls("D14: upstream has no workspace lock; a concurrent run proceeds")
def test_d14_concurrent_mutation_refused(impl, tmp_path):
    ts = mini_tasks()
    ws = fw_ws(tmp_path, ts) if impl == "framework" else (import_upstream() and up_ws(tmp_path, ts))
    holder = _hold(os.path.join(ws, ".hws.lock"))
    try:
        if impl == "framework":
            r = hws("run-task", ws, "tr-0")
            assert r.returncode == 75 and str(holder.pid) in r.stderr
        else:
            from wikiskill import cli as ucli
            rc = ucli.main(["run-task", "x", "tr-0", "--ws", ws])
            assert rc == 75
    finally:
        holder.kill()


def test_d14_lock_context_raises_and_reports_pid(tmp_path):
    ws = str(tmp_path / "w")
    os.makedirs(ws)
    holder = _hold(locking.lock_path(ws))
    try:
        assert locking.is_locked(ws)
        with pytest.raises(WorkspaceBusyError, match=str(holder.pid)):
            with locking.workspace_lock(ws):
                pass
    finally:
        holder.kill()
        holder.wait()
    assert not locking.is_locked(ws)
    with locking.workspace_lock(ws):
        assert locking.holder_pid(locking.lock_path(ws)) == os.getpid()


def test_d14_wait_blocks_until_released(tmp_path):
    ws = str(tmp_path / "w")
    os.makedirs(ws)
    holder = _hold(locking.lock_path(ws), seconds=1.0)
    t0 = time.time()
    with locking.workspace_lock(ws, wait=True):
        assert time.time() - t0 >= 0.5
    holder.wait()


def test_d14_rollout_lock_serializes_rollouts(tmp_path, monkeypatch, answers):
    from hermes_wikiskill import gating
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    lock = tmp_path / "team.lock"
    monkeypatch.setenv("HWS_ROLLOUT_LOCK", str(lock))
    holder = _hold(str(lock), seconds=1.0)
    t0 = time.time()
    gating.run_task(ws, ts[0], 1)
    assert time.time() - t0 >= 0.5  # waited for the host-wide lock
    holder.wait()


def test_d14_max_parallel_above_one_refused(tmp_path):
    ws = fw_ws(tmp_path)
    r = hws("evolve", ws, "--max-parallel", "2")
    assert r.returncode == 2


def test_d14_dry_run_takes_no_lock(tmp_path):
    ws = fw_ws(tmp_path)
    holder = _hold(os.path.join(ws, ".hws.lock"))
    try:
        assert hws("run-task", ws, "tr-0", "--dry-run").returncode == 0
    finally:
        holder.kill()
    assert fcntl  # imported for the holder script symmetry
