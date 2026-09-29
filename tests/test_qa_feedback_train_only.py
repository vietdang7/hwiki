"""qa adversarial (D18/D19): grader feedback reaches the maintainer for TRAIN tasks only; val and test answers never
appear in any agent input (task prompts, maintainer/proposer prompts, wiki, proposals) nor in val/test trace metadata or
eval records."""
import json
import os

from conftest import fw_ws, make_task
from hermes_wikiskill import evaluate, harness, traces


def _tasks():
    tr = [make_task("tr-%d" % i, "train", answer="== TRAIN-ANSWER-%d ==\n" % i) for i in range(2)]
    va = [make_task("va-%d" % i, "val", answer="== VAL-SECRET-%d ==\n" % i) for i in range(2)]
    te = [make_task("te-%d" % i, "test", answer="== TEST-SECRET-%d ==\n" % i) for i in range(2)]
    return tr, va, te


def _agent_inputs(ws):
    """Text an agent is given or maintains: every prompt/query file, the wiki and the proposals (not deliverables)."""
    blob = {}
    for dirpath, dirnames, files in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in (".git", "eval", "bench", "skills")]
        for f in files:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, ws)
            if os.path.islink(p):
                continue
            if f in ("query.txt", "prompt.txt") or rel.startswith(("wiki" + os.sep, os.path.join("runs", "proposals"))):
                blob[rel] = open(p, errors="replace").read()
    return blob


def test_qa_val_and_test_answers_never_reach_agents(tmp_path, answers):
    tr, va, te = _tasks()
    answers(tr + va + te)
    ws = fw_ws(tmp_path, tr + va)          # no skill: the fake writes a naive report -> train fails -> feedback
    (tmp_path / "test.json").write_text(json.dumps(te))
    evaluate.evaluate(ws, str(tmp_path / "test.json"), skills="empty", repeat=1, tag="qa-fb")
    harness.evolve(ws, iters=1, verbose=False)
    inputs = _agent_inputs(ws)
    maint = "".join(v for k, v in inputs.items() if k.startswith(os.path.join("runs", "maintain-01")))
    assert maint and "TRAIN-ANSWER" in maint, "train feedback must reach the maintainer prompt"
    leaks = sorted(k for k, v in inputs.items() if "VAL-SECRET" in v or "TEST-SECRET" in v)
    assert not leaks, "val/test answers reached agent inputs: %s" % leaks
    for t in traces.list_traces(ws):
        if t["split"] in ("val", "test"):
            dump = json.dumps(t["meta"])
            assert "feedback" not in t["meta"] and "SECRET" not in dump, (t["split"], t["task_id"])
    for dirpath, _d, files in os.walk(os.path.join(ws, "eval")):
        for f in files:
            if f.endswith(".json"):
                data = open(os.path.join(dirpath, f), errors="replace").read()
                assert "TEST-SECRET" not in data and '"feedback"' not in data, f
