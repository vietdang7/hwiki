"""D16 compare fairness, D17 config cannot disable skills, D18 TRAIN-only grader feedback,
Q4 session id from stderr, usage recording."""

import json
import os

import pytest

from conftest import add_skill, fw_ws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import compare, gating, harness, prompts, traces
from hermes_wikiskill.backends import hermes
from hermes_wikiskill.errors import SkillsInvisibleError


@impls("D16: upstream pairs workspaces by task id only, ignoring changed prompts/graders")
def test_d16_compare_rejects_same_ids_different_content(impl, tmp_path):
    a_tasks = mini_tasks()
    b_tasks = [dict(t, prompt=t["prompt"] + " CHANGED") if t["split"] == "val" else t for t in a_tasks]
    if impl == "framework":
        a, b = fw_ws(tmp_path, a_tasks, "a"), fw_ws(tmp_path, b_tasks, "b")
        with pytest.raises(ValueError, match="differ in content"):
            compare.run_comparison(a, b, iters=1, dry_run=True)
    else:
        import_upstream()
        from wikiskill import compare as uc
        a, b = up_ws(tmp_path, a_tasks, "a"), up_ws(tmp_path, b_tasks, "b")
        with pytest.raises(ValueError):
            uc.run_comparison(a, b, iters=1, dry_run=True)


def test_d16_compare_runs_are_unique_and_leave_traces_alone(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    a, b = fw_ws(tmp_path, ts, "a"), fw_ws(tmp_path, ts, "b")
    add_skill(a)
    r1 = compare.run_comparison(a, b, iters=2, tag="c1")
    r2 = compare.run_comparison(a, b, iters=1, tag="c2")
    assert r1["pass_rate"] == {"A": 1.0, "B": 0.0}
    assert os.path.isdir(os.path.join(a, "eval", "c1-A")) and os.path.isdir(os.path.join(a, "eval", "c2-A"))
    assert traces.list_traces(a) == [] and r2["eval_tags"]["B"] == "c2-B"


@impls("D17: upstream copies the source config, whose agent.disabled_toolsets removes skills")
def test_d17_source_config_cannot_disable_skills(impl, tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        add_skill(ws)
        r = gating.run_task(ws, ts[0], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        from wikiskill.backends import hermes as uh
        ws = up_ws(tmp_path, ts)
        add_skill(ws)
        uh.DEFAULT_TOOLSETS += ",skills"  # even with the user's hand patch (D4)
        try:
            r = ug.run_task(ws, ts[0], 1)
        finally:
            uh.DEFAULT_TOOLSETS = uh.DEFAULT_TOOLSETS.replace(",skills", "")
    assert r["score"] == 1.0


def test_d17_disabled_skills_in_workspace_config_fails_loudly(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    real = hermes.credentials.write_profile

    def sabotage(home, plan):
        changed = real(home, plan)
        with open(os.path.join(home, "config.yaml"), "a") as f:
            f.write("agent:\n  disabled_toolsets:\n    - skills\n")
        return changed

    hermes.credentials.write_profile = sabotage
    try:
        with pytest.raises(SkillsInvisibleError):
            gating.run_task(ws, ts[0], 1)
    finally:
        hermes.credentials.write_profile = real


def test_d18_train_trace_carries_grader_feedback(tmp_path, answers):
    ts = mini_tasks()
    ws = fw_ws(tmp_path, ts)
    r = gating.run_task(ws, ts[0], 1)  # no skill: fake writes a naive report → fails
    meta = traces.load_trace(ws, 1, "train", ts[0]["id"])
    assert r["score"] == 0.0
    assert meta["feedback"]["expected"] == ts[0]["grader"]["expected"]
    assert meta["feedback"]["got"] == "a naive report\n"
    assert len(json.dumps(meta["feedback"])) <= 2048


def test_d18_val_trace_has_no_feedback(tmp_path):
    ts = mini_tasks()
    ws = fw_ws(tmp_path, ts)
    r = gating.run_task(ws, ts[2], 1)
    assert "feedback" not in traces.load_trace(ws, 1, "val", ts[2]["id"]) and "feedback" not in r


@impls("D18: upstream maintainer prompt carries only trace paths (no expected-vs-got)")
def test_d18_maintainer_prompt_includes_feedback(impl, tmp_path):
    sampled = [{"task_id": "tr-0", "score": 0.0, "title": "t", "path": "/x.jsonl",
                "feedback": {"grader": "exact", "file": "report.txt", "expected": "EXPECTED-XYZ",
                             "got": "GOT-ABC"}}]
    if impl == "framework":
        text = prompts.maintainer_prompt("/ws", 1, sampled)
    else:
        import_upstream()
        from wikiskill import prompts as up
        text = up.maintainer_prompt("/ws", 1, sampled)
    assert "EXPECTED-XYZ" in text and "GOT-ABC" in text


def test_d18_feedback_is_truncated(tmp_path):
    from hermes_wikiskill import scoring
    (tmp_path / "report.txt").write_text("g" * 10000)
    fb = scoring.feedback({"grader": {"type": "exact", "file": "report.txt", "expected": "e" * 10000}},
                          str(tmp_path))
    assert len(json.dumps(fb)) <= 2048


def test_q4_session_id_read_from_stderr(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    r = gating.run_task(ws, ts[0], 1)
    run_dir = os.path.join(ws, "runs", "iter-01", "train", "tr-0")
    assert "session_id" not in open(os.path.join(run_dir, "stdout.txt")).read()
    assert r["result"]["session_id"] in open(os.path.join(run_dir, "stderr.txt")).read()


def test_usage_recorded_for_task_and_framework_rollouts(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=1, verbose=False)
    metas = traces.list_traces(ws)
    assert metas and all(m["meta"]["usage"]["cost_usd"] == 0.001 for m in metas)
    for tag in ("maintain-01", "propose-01"):
        rec = json.load(open(os.path.join(ws, "runs", tag, "run.json")))
        assert rec["usage"]["cost_usd"] == 0.001 and rec["skills_visible"]


def test_proposer_guidance_front_loads_description_for_index_truncation():
    """Host evidence (R1-qa C1): Hermes' <available_skills> line cuts the description at ~60 chars."""
    from hermes_wikiskill import resources, skillfiles
    text = prompts.proposer_prompt("/ws", 1, [])
    assert "FIRST ~60 characters" in text and "≤ 200 chars" in text
    skill = open(os.path.join(resources.framework_skills_root(), "wikiskill-proposer", "SKILL.md")).read()
    assert "FIRST ~60 characters" in skill
    assert skillfiles.INDEX_DESC_VISIBLE == 60
