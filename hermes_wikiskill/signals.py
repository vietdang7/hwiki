"""Signal → SystemExit for every hermes child run (QA-F4 task rollouts, R1-18 role runs).

SIGTERM/SIGHUP normally kill Python without running ``finally`` blocks, leaving the hermes
child orphaned and its temporary home (.env, state.db) behind. Inside ``terminate_as_exit()``
they raise ``SystemExit(128 + signum)`` instead, so the caller's ``finally`` kills the child's
process group and removes the home. SIGINT already raises KeyboardInterrupt. Only the main
thread may install handlers; elsewhere this is a no-op.
"""

from __future__ import annotations

import contextlib
import signal
import threading


@contextlib.contextmanager
def terminate_as_exit():
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    prev = {}

    def _exit(signum, _frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        cur = signal.getsignal(sig)
        if cur in (signal.SIG_DFL, None):
            prev[sig] = cur
            signal.signal(sig, _exit)
    try:
        yield
    finally:
        for sig, cur in prev.items():
            signal.signal(sig, cur if cur is not None else signal.SIG_DFL)


__all__ = ["terminate_as_exit"]
