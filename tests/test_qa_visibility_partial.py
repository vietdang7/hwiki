"""qa adversarial (D1): with TWO active skills, an exported system prompt that lists only one of them must fail loudly
(SkillsInvisibleError naming the missing skill, exit 3, no trace/score) - not pass because *an* index is present."""
import pytest

from conftest import add_skill, fw_ws, hws, mini_tasks
from hermes_wikiskill import gating, traces
from hermes_wikiskill.errors import SkillsInvisibleError
from test_qa_support import qa_hermes


def _ws(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws, "house-report")
    add_skill(ws, "second-skill", body="second skill rules")
    return ws, ts


def test_qa_one_missing_active_name_raises(tmp_path, monkeypatch, answers):
    ws, ts = _ws(tmp_path, answers)
    qa_hermes(tmp_path, monkeypatch, drop="second-skill")
    with pytest.raises(SkillsInvisibleError, match="second-skill"):
        gating.run_task(ws, ts[0], 1)
    assert not traces.exists(ws, 1, "train", ts[0]["id"])


def test_qa_one_missing_active_name_cli_exit_3(tmp_path, monkeypatch, answers):
    ws, ts = _ws(tmp_path, answers)
    qa_hermes(tmp_path, monkeypatch, drop="second-skill")
    r = hws("run-task", ws, ts[2]["id"])
    assert r.returncode == 3, r.stderr
    assert "second-skill" in r.stderr


def test_qa_both_names_listed_passes(tmp_path, monkeypatch, answers):
    ws, ts = _ws(tmp_path, answers)
    qa_hermes(tmp_path, monkeypatch)
    r = gating.run_task(ws, ts[0], 1)
    assert set(r["result"].get("skills_visible") or traces.load_trace(ws, 1, "train", ts[0]["id"])["skills_visible"]) \
        >= {"house-report", "second-skill"}
