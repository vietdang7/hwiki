"""End-to-end Algorithm 1 against the fake Hermes: accept, reject, invalid proposal,
no_action, early stop, resume, and the state/history contract (INTERFACE §3.2)."""

import json
import os

from conftest import fw_ws, hws, make_task, mini_tasks
from hermes_wikiskill import evaluate, gating, gitutil, harness, transfer


def test_evolve_learns_and_accepts(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    state = harness.evolve(ws, iters=2, verbose=False)
    assert state["baseline"] == 0.0 and state["r_best"] == 1.0
    h = state["history"][0]
    assert h["accepted"] is True and h["r_val"] == 1.0 and h["proposal"] == "create house-report"
    assert {"r_best_before", "skills_visible", "ts", "active_commit"} <= set(h)
    assert state["next_iter"] == 2 and len(state["history"]) == 1  # early stop at R_best 1.0
    assert transfer.active_skills(ws) == ["house-report"]
    assert gitutil.is_clean(gating.active_dir(ws))
    assert "ACCEPTED" in open(os.path.join(ws, "wiki", "skill-impact.md")).read()
    assert os.path.isfile(os.path.join(ws, "wiki", "patterns", "fake-pattern.md"))


def test_evolve_rejects_and_rolls_back(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    monkeypatch.setenv("FAKE_HERMES_PROPOSAL", json.dumps(
        {"action": "create", "name": "useless", "skill_md": "---\nname: useless\ndescription: x\n---\nnothing"}))
    state = harness.evolve(ws, iters=1, verbose=False)
    h = state["history"][0]
    assert h["accepted"] is False and h["r_val"] == 0.0
    assert transfer.active_skills(ws) == [] and gitutil.is_clean(gating.active_dir(ws))
    assert "REJECTED" in open(os.path.join(ws, "wiki", "skill-impact.md")).read()


def test_evolve_invalid_proposal_recorded_not_gated(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    monkeypatch.setenv("FAKE_HERMES_PROPOSAL", json.dumps(
        {"action": "create", "name": "../evil", "skill_md": "x"}))
    state = harness.evolve(ws, iters=1, verbose=False)
    h = state["history"][0]
    assert h["accepted"] is False and h["r_val"] is None and h["proposal"].startswith("invalid")
    assert not os.path.exists(os.path.join(ws, "raw", "traces", "iter-01", "val"))
    assert not os.path.exists(os.path.join(ws, "skills", "evil"))


def test_evolve_no_action_and_resume(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    monkeypatch.setenv("FAKE_HERMES_PROPOSAL", "none")
    s1 = harness.evolve(ws, iters=1, verbose=False)
    assert s1["history"] == [] and s1["next_iter"] == 2
    monkeypatch.delenv("FAKE_HERMES_PROPOSAL")
    s2 = harness.evolve(ws, iters=2, verbose=False)  # resumes at iter 2
    assert [h["iter"] for h in s2["history"]] == [2] and s2["r_best"] == 1.0


def test_evolve_iters_zero_is_baseline_only(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    s = harness.evolve(ws, iters=0, verbose=False)
    assert s["baseline"] == 0.0 and s["next_iter"] == 1 and s["history"] == []
    assert not os.path.exists(os.path.join(ws, "raw", "traces", "iter-01"))


def test_learned_skill_improves_heldout_eval(tmp_path, answers):
    ts = mini_tasks()
    test = [make_task(f"te-{i}", "test") for i in range(3)]
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=1, verbose=False)
    tf = tmp_path / "test.json"
    tf.write_text(json.dumps(test))
    empty = evaluate.evaluate(ws, str(tf), skills="empty", repeat=2, tag="empty")
    active = evaluate.evaluate(ws, str(tf), skills="active", repeat=2, tag="active")
    assert active["mean"] - empty["mean"] == 1.0
    assert active["active_commit"] == gitutil.head(gating.active_dir(ws))


def test_cli_evolve_and_status_json(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    tf = tmp_path / "tasks.json"
    tf.write_text(json.dumps(ts))
    root = tmp_path / "wsroot"
    assert hws("--ws-root", str(root), "init", "demo", "--tasks", str(tf)).returncode == 0
    r = hws("--ws-root", str(root), "evolve", "demo", "--iters", "1", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["r_best"] == 1.0
    st = json.loads(hws("status", str(root / "demo"), "--json").stdout)
    assert st["active_skills"] == ["house-report"] and st["tasks"] == {"train": 2, "val": 2}
    assert st["usage"]["cost_usd"] > 0 and st["locked"] is False
    human = hws("--ws-root", str(root), "status", "demo")
    assert human.returncode == 0 and "house-report" in human.stdout
