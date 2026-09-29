"""qa adversarial (D19 / D5): findings QA-F2 and QA-F3 in reports/QA-REVIEW-T2.md.

QA-F2  escape detection matches literal markers (workspace path, "hermes-wikiskill", basenames tasks.json/test.json)
       in tool-call ARGUMENTS only; rollouts inherit HOME, so ordinary shell idioms (globs, find -exec) reach a
       workspace's graded outputs unflagged.
QA-F3  rollouts inherit the caller's whole environment: a secret the caller holds under a name Hermes does not know
       (the host's .env has such custom names) reaches every task agent, whose terminal can print it.
Both were fixed by core in f409200 (tool results + resolved paths scanned, HOME = rollout home, allowlisted hermes
env); these tests now guard the fixes."""
import json
import os

import pytest

from conftest import fw_ws, mini_tasks
from hermes_wikiskill import gating, rollout
from test_qa_support import chat_homes, qa_hermes


def _session(command, result):
    return {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": command})}}]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "terminal", "content": json.dumps({"output": result})}]}


BYPASSES = {
    "find-exec-cat": "find ~ -name report.txt -print -exec cat {} +",
    "glob-graded-outputs": "cat ~/ops/*/workspaces/*/runs/iter-*/val/*/sandbox/report.txt",
    "glob-answer-key": "python3 -c \"import glob; print(open(glob.glob('/home/*/ops/*/workspaces/*/task?.json')[0]).read())\"",
}


def test_qa_literal_escape_is_flagged(tmp_path):
    """Control: the literal workspace path in a tool call is flagged."""
    ws = str(tmp_path / "ws")
    assert rollout.detect_escapes(_session("cat %s/runs/iter-01/val/va-0/sandbox/report.txt" % ws, "x"), ws)


@pytest.mark.parametrize("kind", sorted(BYPASSES))
def test_qa_escape_without_literal_marker_is_flagged(tmp_path, kind):
    """The tool RESULT shows the agent reached the workspace (its path and a graded answer); only the argument is
    free of markers."""
    ws = str(tmp_path / "ws")
    seen = "%s/runs/iter-01/val/va-0/sandbox/report.txt\n== VAL ANSWER ==\n" % ws
    assert rollout.detect_escapes(_session(BYPASSES[kind], seen), ws)


def test_qa_task_rollout_home_is_not_the_operator_home(tmp_path, monkeypatch, answers):
    """`~` in a task rollout expands to the caller's HOME, where ~/ops/hermes-wikiskill/workspaces lives."""
    log = qa_hermes(tmp_path, monkeypatch)
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[2], 1)
    task = [h for h in chat_homes(log) if not os.path.realpath(h["home"]).startswith(os.path.realpath(ws))]
    assert task and task[-1]["home_env"] != os.environ.get("HOME")


def test_qa_caller_env_secrets_do_not_reach_task_rollouts(tmp_path, monkeypatch, answers):
    monkeypatch.setenv("QA_CUSTOM_API_KEY", "fake-custom")      # unregistered name: Hermes' own scrub keeps it
    monkeypatch.setenv("QA_CUSTOMBOT_TOKEN", "fake-bot-env")
    log = qa_hermes(tmp_path, monkeypatch)
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[2], 1)
    homes = chat_homes(log)
    assert homes
    for h in homes:
        assert not {"QA_CUSTOM_API_KEY", "QA_CUSTOMBOT_TOKEN"} & set(h["env_names"]), h["home"]
