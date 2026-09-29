"""qa adversarial (D8/D19, SPEC R4 "test split NEVER used by maintainer/proposer"): QA-F8 in .cmux-team/QA-REPORT.md.

Live receipt (framework acceptance run framework-20260928T143046Z, maintainer session 20260928_213501_e86a1d): the
maintainer read files OUTSIDE its workspace - the held-out test file whose absolute path the eval summary records,
another workspace's answer key, and the bench generator + held-out split under ~/ops/hermes-wikiskill-src. Roles run
unconfined (caller HOME, any path) and heldout.detect_access only knows <ws>/eval paths and the ids of evals that
already ran in THIS ws, so in a fresh workspace such reads went unflagged. Fixed by core T8 (xfail markers removed by core).

The "outside" files live in a /tmp/hws-qa-f8-* dir: pytest's tmp_path is under /tmp on Linux (allowlisted scratch per
core-T8 A), while /tmp/hws-* is never allowlisted - so the pins mean the same on macOS and on the host (C2)."""
import json
import os
import pathlib
import shutil
import tempfile

import pytest

from conftest import add_skill, fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, harness, tasks as tasks_mod



@pytest.fixture
def outside():
    d = pathlib.Path(tempfile.mkdtemp(prefix="hws-qa-f8-", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _heldout_outside(outside):
    """A held-out split outside the ws, like ~/ops/hermes-wikiskill-src/benches/house-report/test.json on the host."""
    te = [make_task("te-out-%d" % i, "test", answer="== QA-F8-ANSWER-%d ==\n" % i) for i in range(2)]
    p = outside / "bench-src" / "house-report" / "test.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(tasks_mod.seal_split(te)))
    return str(p)


def _evolve_with_role_read(tmp_path, answers, monkeypatch, role, path):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)                     # fresh ws: no eval ran here, so no held-out id is known to it
    monkeypatch.setenv("FAKE_HERMES_ROLE_READ", "%s:%s" % (role, path))
    return harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]


@pytest.mark.parametrize("role", ["maintainer", "proposer"])
def test_qa_role_reading_heldout_split_outside_ws_invalidates_iteration(tmp_path, answers, monkeypatch, outside, role):
    h = _evolve_with_role_read(tmp_path, answers, monkeypatch, role, _heldout_outside(outside))
    assert h["r_val"] is None and not h["accepted"] and role in (h["proposal"] or "")


def test_qa_role_reading_another_workspace_invalidates_iteration(tmp_path, answers, monkeypatch, outside):
    other = fw_ws(outside, mini_tasks(), name="other-ws")        # a sibling workspace (its own tasks.json)
    h = _evolve_with_role_read(tmp_path, answers, monkeypatch, "maintainer", os.path.join(other, "tasks.json"))
    assert h["r_val"] is None and not h["accepted"]


def test_qa_role_reading_inside_its_ws_still_gates(tmp_path, answers, monkeypatch):
    """Control: an ordinary in-workspace read must not be treated as contamination (guards an over-broad fix)."""
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    monkeypatch.setenv("FAKE_HERMES_ROLE_READ", "maintainer:" + os.path.join(ws, "wiki", "index.md"))
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert h["r_val"] is not None and "contaminated" not in (h["proposal"] or "")


def test_qa_eval_summary_does_not_point_at_the_heldout_file(tmp_path, answers, outside):
    """The eval summary is readable by later maintainer runs; it must not hand them the held-out file's location."""
    ts, te = mini_tasks(), [make_task("te-0", "test")]
    answers(ts + te)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = outside / "held-out-dir" / "test.json"
    tf.parent.mkdir()
    tf.write_text(json.dumps(tasks_mod.seal_split(te)))
    evaluate.evaluate(ws, str(tf), skills="active", repeat=1, tag="qa-f8")
    raw = open(os.path.join(ws, "eval", "qa-f8", "summary.json")).read()
    assert str(tf.parent) not in raw and os.path.realpath(str(tf.parent)) not in raw
