"""qa adversarial (D3/D2): every --dry-run is a ZERO-write plan on a workspace that already has history - no path, byte,
mode, mtime, symlink or git ref changes in either workspace or HWS_HOME, and no Hermes rollout is launched."""
import json
import os

import pytest

from conftest import add_skill, fake_log, fw_ws, hws, make_task, mini_tasks
from hermes_wikiskill import gating
from test_qa_support import snapshot_diff, strict_snapshot

VERBS = [
    ["run-task", "{ws}", "tr-0"],
    ["run-task", "{ws}", "tr-0", "--iter", "1"],          # same iter as the existing real trace
    ["gate", "{ws}"],
    ["maintain", "{ws}"],
    ["propose", "{ws}"],
    ["evolve", "{ws}", "--iters", "2"],
    ["reset", "{ws}"],
    ["bench", "{ws}", "--tasks", "{tasks}"],
    ["eval", "{ws}", "--tasks", "{test}"],
    ["compare", "{ws}", "{ws2}", "--iters", "1"],
    ["transfer", "{ws}", "{ws2}"],
]


@pytest.fixture
def lived_in(tmp_path, answers):
    ts = mini_tasks()
    test = [make_task("te-0", "test")]
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    ws2 = fw_ws(tmp_path, ts, "ws2")
    add_skill(ws)
    gating.run_task(ws, ts[0], 1)          # a real trace, runs/ entries and a session already exist
    (tmp_path / "t.json").write_text(json.dumps([dict(t, prompt=t["prompt"] + " v2") for t in ts]))
    (tmp_path / "test.json").write_text(json.dumps(test))
    return {"ws": ws, "ws2": ws2, "tasks": str(tmp_path / "t.json"), "test": str(tmp_path / "test.json")}


def _rollouts(tmp_path):
    return [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]]


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: "-".join(x for x in v if not x.startswith("{")))
def test_qa_dry_run_writes_nothing_strict(verb, lived_in, tmp_path):
    roots = [lived_in["ws"], lived_in["ws2"], os.environ["HWS_HOME"]]
    before = {r: strict_snapshot(r) for r in roots}
    n = len(_rollouts(tmp_path))
    r = hws(*[a.format(**lived_in) for a in verb], "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout)
    for root in roots:
        diff = snapshot_diff(before[root], strict_snapshot(root))
        assert not diff, "%s --dry-run changed %s: %s" % (verb[0], root, sorted(diff)[:8])
    assert len(_rollouts(tmp_path)) == n, "%s --dry-run launched a Hermes rollout" % verb[0]
