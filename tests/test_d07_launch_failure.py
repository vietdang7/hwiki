"""D7: a Hermes launch/argument failure is an error, never a 0.0 score fed to the gate."""

import pytest

from conftest import fw_ws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import gating, gitutil, harness, traces
from hermes_wikiskill.errors import RolloutError


@impls("D7: upstream grades the sandbox after a failed launch (score 0.0 recorded)")
def test_d07_launch_failure_not_scored(impl, tmp_path, monkeypatch):
    ts = mini_tasks()
    monkeypatch.setenv("FAKE_HERMES_EXIT", "2")
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        with pytest.raises(RolloutError):
            gating.run_task(ws, ts[0], 1)
        assert not traces.exists(ws, 1, "train", ts[0]["id"])
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        r = ug.run_task(ws, ts[0], 1)
        assert r["score"] is None  # upstream records 0.0 → xfail


def test_d07_gate_failure_rolls_back_and_makes_no_decision(tmp_path, monkeypatch, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=0, verbose=False)  # baseline

    real = gating.run_gate

    def failing_gate(*a, **k):
        monkeypatch.setenv("FAKE_HERMES_EXIT", "2")
        return real(*a, **k)

    monkeypatch.setattr(gating, "run_gate", failing_gate)
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False)
    state = gating.load_state(ws)
    assert state["next_iter"] == 1 and state["history"] == []
    assert gitutil.is_clean(gating.active_dir(ws))
    assert not any(n for n in __import__("os").listdir(gating.active_dir(ws)) if not n.startswith("."))
