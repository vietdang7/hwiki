"""D8: held-out test tasks live outside workspaces and are only run by `eval`, which writes
only under <ws>/eval/<tag>/ and never touches skills/active, traces, state or wiki."""

import json
import os

import pytest

from conftest import add_skill, fw_ws, hws, impls, import_upstream, make_task, mini_tasks, tree_hash
from hermes_wikiskill import evaluate, harness, tasks as tasks_mod, traces


def _test_file(tmp_path, n=2):
    ts = [make_task(f"te-{i}", "test") for i in range(n)]
    p = tmp_path / "test.json"
    p.write_text(json.dumps(ts))
    return p, ts


@impls("D8: upstream validate rejects split=test — no held-out split exists at all")
def test_d08_test_split_loadable_for_eval(impl, tmp_path):
    p, ts = _test_file(tmp_path)
    if impl == "framework":
        assert tasks_mod.load_file(str(p)) == ts
    else:
        import_upstream()
        from wikiskill import tasks as ut
        ut.validate(ts)


def test_d08_workspace_refuses_test_tasks(tmp_path):
    with pytest.raises(ValueError, match="held-out"):
        harness.init_workspace(str(tmp_path / "ws"), mini_tasks() + [make_task("te-0", "test")])
    assert not (tmp_path / "ws").exists()


def _without_eval(ws):
    import shutil
    snap = ws + ".snap"
    shutil.copytree(ws, snap, symlinks=True, ignore=shutil.ignore_patterns("eval", ".hws.lock"))
    return tree_hash(snap)


def test_d08_eval_writes_only_under_eval_dir(tmp_path, answers):
    ts = mini_tasks()
    p, test = _test_file(tmp_path)
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    before = _without_eval(ws)
    import shutil
    shutil.rmtree(ws + ".snap")
    s = evaluate.evaluate(ws, str(p), skills="active", repeat=2, tag="t1")
    assert _without_eval(ws) == before
    assert s["mean"] == 1.0 and len(s["repeats"]) == 2 and s["skill_names"] == ["house-report"]
    assert os.path.isfile(os.path.join(ws, "eval", "t1", "summary.json"))
    assert s["repeats"][0]["results"][0]["skill_views"] == ["house-report"]


def test_d08_eval_empty_skills_sees_no_active_skill(tmp_path, answers):
    ts = mini_tasks()
    p, test = _test_file(tmp_path)
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    s = evaluate.evaluate(ws, str(p), skills="empty", tag="e1")
    assert s["mean"] == 0.0 and s["skill_names"] == []
    assert all(r["skill_views"] == [] for r in s["repeats"][0]["results"])


def test_d08_eval_summary_schema_and_tag_collision(tmp_path, answers):
    ts = mini_tasks()
    p, test = _test_file(tmp_path)
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    r = hws("eval", ws, "--tasks", str(p), "--skills", "empty", "--tag", "x", "--json")
    assert r.returncode == 0, r.stderr
    s = json.loads(r.stdout)
    for k in ("tag", "ws", "skills", "skill_names", "active_commit", "model", "hermes_version",
              "max_turns", "tasks_file", "tasks_sha256", "repeats", "mean", "started", "finished"):
        assert k in s
    res = s["repeats"][0]["results"][0]
    assert set(res) >= {"task_id", "score", "duration_s", "exit_code", "usage", "skill_views", "session"}
    assert res["usage"]["cost_usd"] == 0.001
    r2 = hws("eval", ws, "--tasks", str(p), "--skills", "empty", "--tag", "x")
    assert r2.returncode == 2 and "exists" in r2.stderr


def test_d08_evolve_never_sees_test_tasks(tmp_path, answers):
    """Eval FIRST (test data now exists under <ws>/eval/<tag>), then evolve: no test id, test
    prompt or eval path may reach maintainer/proposer inputs, wiki, proposals or traces."""
    ts = mini_tasks()
    test = [make_task(f"heldout-{i}", "test") for i in range(2)]
    for t in test:
        t["prompt"] = f"SECRET-HELDOUT-PROMPT {t['id']}"
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    tf = tmp_path / "test.json"
    tf.write_text(json.dumps(test))
    s = evaluate.evaluate(ws, str(tf), skills="empty", tag="pre")
    assert os.path.isfile(os.path.join(ws, "eval", "pre", "summary.json")) and len(s["repeats"][0]["results"]) == 2
    harness.evolve(ws, iters=1, verbose=False)
    needles = ["heldout-", "SECRET-HELDOUT-PROMPT", os.path.join(ws, "eval"), str(tf)]
    scanned = 0
    for sub in ("runs/maintain-01", "runs/propose-01", "runs/proposals", "wiki", "raw/traces"):
        for root, dirs, files in os.walk(os.path.join(ws, sub)):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                scanned += 1
                for n in needles:
                    assert n not in text, (n, os.path.join(root, f))
    for split in ("train", "val"):
        assert all(t["task_id"].startswith(("tr-", "va-")) for t in traces.list_traces(ws, split=split))
    assert scanned > 10  # maintainer + proposer query/session, proposal, wiki pages, traces
