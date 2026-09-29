"""qa adversarial (D14): EVERY mutating verb refuses a workspace another process holds (exit 75, holder pid, nothing
written), and read-only verbs keep working while it is held. Core's tests exercise run-task only."""
import json
import os

import pytest

from conftest import add_skill, fw_ws, hws, make_task, mini_tasks
from hermes_wikiskill import locking
from test_qa_support import hold_lock, snapshot_diff, strict_snapshot

MUTATING = [
    ["run-task", "{ws}", "tr-0"],
    ["gate", "{ws}"],
    ["maintain", "{ws}"],
    ["propose", "{ws}"],
    ["evolve", "{ws}", "--iters", "1"],
    ["reset", "{ws}"],
    ["bench", "{ws}", "--tasks", "{tasks}"],
    ["eval", "{ws}", "--tasks", "{test}"],
    ["transfer", "{ws2}", "{ws}"],          # the destination is the locked workspace
    ["compare", "{ws}", "{ws2}", "--iters", "1"],
]


@pytest.fixture
def held(tmp_path, answers):
    ts = mini_tasks()
    test = [make_task("te-0", "test")]
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    ws2 = fw_ws(tmp_path, ts, "ws2")
    add_skill(ws2)
    (tmp_path / "t.json").write_text(json.dumps(ts))
    (tmp_path / "test.json").write_text(json.dumps(test))
    holder = hold_lock(locking.lock_path(ws))
    yield {"ws": ws, "ws2": ws2, "tasks": str(tmp_path / "t.json"), "test": str(tmp_path / "test.json"), "pid": holder.pid}
    holder.kill()
    holder.wait()


@pytest.mark.parametrize("verb", MUTATING, ids=lambda v: v[0])
def test_qa_mutating_verb_refuses_held_workspace(verb, held):
    before = strict_snapshot(held["ws"])
    r = hws(*[a.format(**held) for a in verb])
    assert r.returncode == 75, "%s rc=%s stderr=%s" % (verb[0], r.returncode, r.stderr[-300:])
    assert str(held["pid"]) in r.stderr
    diff = {k: v for k, v in snapshot_diff(before, strict_snapshot(held["ws"])).items() if k != ".hws.lock"}
    assert not diff, "%s wrote into a workspace it could not lock: %s" % (verb[0], sorted(diff)[:6])


@pytest.mark.parametrize("verb", [["status", "{ws}"], ["status", "{ws}", "--json"]], ids=lambda v: "-".join(v[2:]) or "text")
def test_qa_read_only_verbs_work_while_held(verb, held):
    r = hws(*[a.format(**held) for a in verb])
    assert r.returncode == 0, r.stderr
    assert os.path.exists(locking.lock_path(held["ws"]))
