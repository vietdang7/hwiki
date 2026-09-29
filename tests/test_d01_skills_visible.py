"""D1: active skills must be visible to the agent in every rollout (skills toolset +
<available_skills> proof in the exported session), else no score (exit 3)."""

import os

import pytest

from conftest import add_skill, fake_log, fw_ws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import gating, harness, sessions, traces
from hermes_wikiskill.backends import hermes
from hermes_wikiskill.errors import SkillsInvisibleError


def _toolsets(argv):
    return argv[argv.index("-t") + 1].split(",") if "-t" in argv else []


@impls("D1: upstream inference toolsets = terminal,file,code_execution (no skills)")
def test_d01_inference_rollout_enables_skills_toolset(impl, tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        add_skill(ws)
        gating.run_task(ws, ts[0], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        add_skill(ws)
        ug.run_task(ws, ts[0], 1)
    chats = [r["argv"] for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"]]
    assert chats and "skills" in _toolsets(chats[-1])


@impls("D1: upstream never checks the session; the active skill is not in <available_skills>")
def test_d01_active_skill_listed_in_session_prompt(impl, tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        add_skill(ws)
        gating.run_task(ws, ts[0], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        add_skill(ws)
        ug.run_task(ws, ts[0], 1)
    session = sessions.load(traces.transcript_path(ws, 1, "train", ts[0]["id"]))
    assert "house-report" in (sessions.available_skills(session.get("system_prompt")) or set())


@impls("D1: upstream maintainer/proposer toolsets are file,terminal (framework skills invisible)")
def test_d01_maintainer_and_proposer_see_framework_skills(impl, tmp_path):
    if impl == "framework":
        tool = [hermes.MAINTAINER_TOOLSETS, hermes.PROPOSER_TOOLSETS]
    else:
        import_upstream()
        from wikiskill.backends import hermes as uh
        tool = [uh.MAINTAINER_TOOLSETS, uh.PROPOSER_TOOLSETS]
    assert all("skills" in t.split(",") for t in tool)


def test_d01_invisible_skill_raises_and_is_not_scored(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    monkeypatch.setenv("FAKE_HERMES_HIDE_SKILLS", "1")
    with pytest.raises(SkillsInvisibleError):
        gating.run_task(ws, ts[0], 1)
    assert not traces.exists(ws, 1, "train", ts[0]["id"])


def test_d01_maintainer_rollout_verifies_framework_skills(tmp_path, monkeypatch):
    ws = fw_ws(tmp_path)
    res = harness.maintain_step(ws, 1, [])
    assert set(res["skills_visible"]) >= {"wikiskill-maintainer", "wikiskill-proposer"}
    monkeypatch.setenv("FAKE_HERMES_HIDE_SKILLS", "1")
    with pytest.raises(SkillsInvisibleError):
        harness.maintain_step(ws, 2, [])


def test_d01_trace_meta_records_visibility_and_views(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    gating.run_task(ws, ts[2], 1)
    meta = traces.load_trace(ws, 1, "val", ts[2]["id"])
    assert meta["skills_expected"] == ["house-report"]
    assert meta["skills_visible"] == ["house-report"]
    assert meta["skill_views"] == ["house-report"]
    assert meta["score"] == 1.0


def test_d01_cli_exit_code_3_when_invisible(tmp_path, answers, monkeypatch):
    from conftest import hws
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    r = hws("run-task", ws, ts[0]["id"], env={"FAKE_HERMES_HIDE_SKILLS": "1"})
    assert r.returncode == 3, r.stderr
    assert "invisible" in r.stderr


def test_d01_available_skills_parser_formats():
    prompt = ("x\n<available_skills>\n  reports: ACME\n    - house-report: ACME format\n"
              "    - plain\n  misc [names only]: a, b\n</available_skills>")
    names = sessions.available_skills(prompt)
    assert {"house-report", "plain", "a", "b"} <= names
    assert sessions.available_skills("no index here") is None
    assert os.path.basename(hermes.profile_dir("/w")) == ".hermes-home"


def test_d01_parses_host_confirmed_export_shape(tmp_path):
    """Shape confirmed on the host by qa (DECISIONS [qa] export shape, closes R1-qa C2)."""
    import json
    session = {
        "id": "20260928_190501_a1b2c3d4", "source": "oneshot", "model": "deepseek-v4-flash",
        "system_prompt": "## Skills\n...\n<available_skills>\n  house-report: ACME\n"
                         "    - house-report: ACME house report format\n</available_skills>",
        "messages": [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "call_id": "c1", "type": "function",
                 "function": {"name": "skill_view", "arguments": "{\"name\": \"house-report\"}"}}]},
            {"role": "tool", "tool_name": "skill_view", "tool_call_id": "c1", "content": "{}"},
            {"role": "assistant", "content": "done"}],
        "tool_call_count": 1, "input_tokens": 1200, "output_tokens": 80,
        "estimated_cost_usd": 0.0004, "actual_cost_usd": None}
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(session) + "\n")
    s = sessions.load(str(p))
    assert sessions.available_skills(s["system_prompt"]) >= {"house-report"}
    assert sessions.skill_views(s) == ["house-report"]
    assert sessions.tool_call_count(s) == 1
    u = sessions.usage(s)
    assert u["cost_usd"] == 0.0004 and u["input_tokens"] == 1200 and u["output_tokens"] == 80
    assert sessions.session_id_from_text("x\nsession_id: 20260928_190501_a1b2c3d4\n") == session["id"]
