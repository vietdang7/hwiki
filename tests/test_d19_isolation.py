"""D19 (CRITICAL, host receipt): answer-key leakage. Task rollouts run outside the workspace,
val/test graders are sealed (sha256), and tool calls reaching for the workspace or an answer-key
file are flagged + scored 0."""

import hashlib
import json
import os

import pytest

from conftest import add_skill, fake_log, fw_ws, hws, impls, import_upstream, make_task, mini_tasks, up_ws
from hermes_wikiskill import evaluate, gating, harness, rollout, scoring, tasks as tasks_mod, traces
from hermes_wikiskill.errors import HwsError, RolloutError


def _chat(tmp_path):
    return [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"]][-1]


def _in_dir(argv):
    return argv[argv.index("--in") + 1] if "--in" in argv else None


@impls("D19: upstream runs the agent in <ws>/bench/tasks/<id>, next to <ws>/tasks.json")
def test_d19_rollout_sandbox_outside_workspace(impl, tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        gating.run_task(ws, ts[2], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        ug.run_task(ws, ts[2], 1)
    rec = _chat(tmp_path)
    for p in (_in_dir(rec["argv"]), rec["env"]["HERMES_HOME"]):
        assert not os.path.realpath(p).startswith(os.path.realpath(ws))


def test_d19_rollout_dir_removed_after_success_and_error(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    root = tmp_path / "rollouts"
    gating.run_task(ws, ts[0], 1)
    rec = _chat(tmp_path)
    assert rec["env"]["HERMES_HOME"].startswith(str(root)) and not root.exists()
    monkeypatch.setenv("FAKE_HERMES_EXIT", "2")
    with pytest.raises(RolloutError):
        gating.run_task(ws, ts[1], 1)
    assert not root.exists()
    assert os.path.isfile(os.path.join(ws, "runs", "iter-01", "train", "tr-1", "stderr.txt"))


def test_d19_prompt_names_no_workspace_path_and_skills_are_copies(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    r = gating.run_task(ws, ts[2], 1)
    run_dir = os.path.join(ws, "runs", "iter-01", "val", ts[2]["id"])
    q = open(os.path.join(run_dir, "query.txt")).read()
    assert ws not in q and os.path.realpath(ws) not in q and "hermes-wikiskill" not in q
    assert r["score"] == 1.0 and r["result"]["skills_visible"] == ["house-report"]
    # QA-F1: a graded VAL deliverable is never kept in the ws, only its digest
    assert not os.path.exists(os.path.join(run_dir, "sandbox"))
    kept = json.load(open(os.path.join(run_dir, "deliverables.json")))["report.txt"]
    assert kept["sha256"] == hashlib.sha256(ts[2]["grader"]["expected"].encode()).hexdigest()
    meta = traces.load_trace(ws, 1, "val", ts[2]["id"])
    assert meta["isolated"] is True and meta["sandbox_escape"] == []


def test_d19_rollout_root_inside_workspace_refused(tmp_path, monkeypatch):
    ws = fw_ws(tmp_path)
    monkeypatch.setenv("HWS_ROLLOUT_ROOT", os.path.join(ws, "tmp"))
    with pytest.raises(HwsError, match="outside"):
        rollout.rollout_root(ws)
    monkeypatch.setenv("HWS_ROLLOUT_ROOT", str(tmp_path / "hws" / "r"))
    with pytest.raises(HwsError):
        rollout.rollout_root(ws)


def test_d19_escape_to_answer_key_scored_zero_and_flagged(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    monkeypatch.setenv("FAKE_HERMES_CHEAT", os.path.join(ws, "tasks.json"))
    r = gating.run_task(ws, ts[2], 1)  # the fake cheats: reads the key AND writes the right answer
    assert r["score"] == 0.0 and r["escapes"]
    meta = traces.load_trace(ws, 1, "val", ts[2]["id"])
    assert meta["score"] == 0.0 and "tasks.json" in meta["sandbox_escape"][0]
    assert json.load(open(os.path.join(ws, "runs", "iter-01", "val", ts[2]["id"], "run.json")))["sandbox_escape"]


def test_d19_escapes_counted_in_eval_and_history(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    test = [make_task("te-0", "test")]
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    tf = tmp_path / "held" / "test.json"
    tf.parent.mkdir()
    tf.write_text(json.dumps(test))
    monkeypatch.setenv("FAKE_HERMES_CHEAT", str(tf))
    s = evaluate.evaluate(ws, str(tf), skills="empty", tag="x")
    assert s["escapes"] == 1 and s["mean"] == 0.0
    assert s["repeats"][0]["results"][0]["sandbox_escape"]
    st = harness.evolve(ws, iters=1, verbose=False)
    assert st["baseline_escapes"] == 2 and st["history"][0]["escapes"] >= 2


def test_d19_detect_escapes_patterns(tmp_path):
    ws = str(tmp_path / "ws")
    def sess(args):
        return {"messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "terminal", "arguments": json.dumps(args)}}]}]}
    assert rollout.detect_escapes(sess({"command": f"cat {ws}/wiki/index.md"}), ws)
    assert rollout.detect_escapes(sess({"command": "find / -name tasks.json"}), ws)
    assert rollout.detect_escapes(sess({"path": "/home/u/ops/hermes-wikiskill-src/benches/x"}), ws)
    assert rollout.detect_escapes(sess({"path": "../../test.json"}), ws)
    assert not rollout.detect_escapes(sess({"path": "/tmp/hws-rollouts/r-1/sandbox/sales.csv"}), ws)
    assert not rollout.detect_escapes(sess({"path": "report.txt", "content": "my_tasks.json_like"}), ws)


def test_d19_init_seals_val_keeps_train_plaintext(tmp_path):
    ts = mini_tasks()
    ws = fw_ws(tmp_path, ts)
    raw = open(os.path.join(ws, "tasks.json")).read()
    saved = json.loads(raw)
    for t in saved:
        if t["split"] == "val":
            assert "expected" not in t["grader"] and len(t["grader"]["expected_sha256"]) == 64
        else:
            assert t["grader"]["expected"] == "== OK ==\n"
    assert raw.count("== OK ==") == 2  # the two train tasks only


def test_d19_sealed_grading_matches_and_mismatches(tmp_path):
    t = tasks_mod.seal(make_task("v", "val", answer="== V ==\nA | 1.00\n"))
    (tmp_path / "report.txt").write_text("== V ==   \nA | 1.00")  # whitespace-normalized equal
    assert scoring.grade(t, str(tmp_path)) == 1.0
    (tmp_path / "report.txt").write_text("== V ==\nA | 2.00\n")
    assert scoring.grade(t, str(tmp_path)) == 0.0
    fb = scoring.feedback(t, str(tmp_path))
    assert fb["expected"] == "<sealed>" and "1.00" not in json.dumps(fb)


def test_d19_validate_sealed_shapes():
    t = make_task("v", "test")
    ui_shape = {"type": "exact", "file": "report.txt",
                "expected_sha256": scoring.seal_digest(t["grader"]["expected"])}
    tasks_mod.validate([{**t, "grader": ui_shape}], allowed_splits=tasks_mod.ALL_SPLITS)
    with pytest.raises(ValueError):
        tasks_mod.validate([{**t, "grader": {**ui_shape, "expected": "x"}}], allowed_splits=("test",))
    with pytest.raises(ValueError):
        tasks_mod.validate([{**t, "grader": {**ui_shape, "expected_sha256": "nothex"}}],
                           allowed_splits=("test",))
    with pytest.raises(ValueError):
        tasks_mod.validate([{**t, "grader": {"type": "exact", "file": "r.txt"}}], allowed_splits=("test",))


def test_d19_eval_accepts_sealed_test_file(tmp_path, answers):
    ts = mini_tasks()
    test = [make_task(f"te-{i}", "test") for i in range(2)]
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = tmp_path / "test.json"
    tf.write_text(json.dumps(tasks_mod.seal_split(test)))
    assert "== OK ==" not in tf.read_text()
    r = hws("eval", ws, "--tasks", str(tf), "--skills", "active", "--tag", "sealed", "--json")
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    assert s["mean"] == 1.0 and s["escapes"] == 0 and s["isolated"] is True
    assert not (tmp_path / "rollouts").exists()
