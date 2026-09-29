"""D2/D3: dry-runs are pure (whole tree + git refs hashed) and never break a later real run."""

import pytest

from conftest import add_skill, fw_ws, hws, impls, import_upstream, mini_tasks, tree_hash, up_ws
from hermes_wikiskill import gating, harness, traces


@impls("D2: upstream dry-run saves a score:null trace, the next real run raises FileExistsError")
def test_d02_dry_run_then_real_run_succeeds(impl, tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        gating.run_task(ws, ts[0], 1, dry_run=True)
        r = gating.run_task(ws, ts[0], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        ug.run_task(ws, ts[0], 1, dry_run=True)
        r = ug.run_task(ws, ts[0], 1)
    assert isinstance(r["score"], float)


def test_d02_real_trace_never_silently_overwritten(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[0], 1)
    before = traces.load_trace(ws, 1, "train", ts[0]["id"])
    with pytest.raises(FileExistsError):
        gating.run_task(ws, ts[0], 1)
    assert traces.load_trace(ws, 1, "train", ts[0]["id"]) == before
    gating.run_task(ws, ts[0], 1, overwrite=True)  # explicit overwrite allowed


@impls("D3: upstream run-task dry-run materializes the sandbox, writes query.txt and symlinks")
def test_d03_run_task_dry_run_is_pure(impl, tmp_path):
    ts = mini_tasks()
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        add_skill(ws)
        h0 = tree_hash(ws)
        gating.run_task(ws, ts[0], 1, dry_run=True)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        add_skill(ws)
        h0 = tree_hash(ws)
        ug.run_task(ws, ts[0], 1, dry_run=True)
    assert tree_hash(ws) == h0


@impls("D3: upstream evolve --dry-run writes state, wiki log/commit, query files")
def test_d03_evolve_dry_run_is_pure(impl, tmp_path):
    ts = mini_tasks()
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        h0 = tree_hash(ws)
        plan = harness.evolve(ws, iters=2, dry_run=True, verbose=False)
        assert plan["dry_run"] and plan["baseline_needed"] and plan["iterations"] == [1, 2]
    else:
        import_upstream()
        from wikiskill import harness as uh
        ws = up_ws(tmp_path, ts)
        h0 = tree_hash(ws)
        uh.evolve(ws, iters=2, dry_run=True, verbose=False)
    assert tree_hash(ws) == h0


@pytest.mark.parametrize("verb", [
    ["run-task", "{ws}", "tr-0"], ["gate", "{ws}"], ["maintain", "{ws}"], ["propose", "{ws}"],
    ["evolve", "{ws}", "--iters", "2"], ["reset", "{ws}"], ["bench", "{ws}", "--tasks", "{tasks}"],
    ["eval", "{ws}", "--tasks", "{tasks}"],
])
def test_d03_every_cli_dry_run_is_pure(verb, tmp_path):
    import json
    ts = mini_tasks()
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = tmp_path / "t.json"
    tf.write_text(json.dumps(ts))
    h0 = tree_hash(ws)
    r = hws(*[a.format(ws=ws, tasks=tf) for a in verb], "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    json.loads(r.stdout)
    assert tree_hash(ws) == h0


def test_d03_init_dry_run_creates_nothing(tmp_path):
    import json
    tf = tmp_path / "t.json"
    tf.write_text(json.dumps(mini_tasks()))
    ws = tmp_path / "hws-x" / "ws"
    r = hws("init", str(ws), "--tasks", str(tf), "--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["dry_run"] is True
    assert not (tmp_path / "hws-x").exists()
