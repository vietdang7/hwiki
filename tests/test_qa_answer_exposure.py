"""qa adversarial (D19/D8, finding QA-F1 in reports/QA-REVIEW-T2.md): sealing val/test graders hides the answer key,
but a graded deliverable that scored 1.0 IS the answer. Maintainer/proposer rollouts run inside the workspace with
file+terminal tools, so no val/test answer may stay readable there after a gate or an eval. QA-F1 was fixed by core
in f409200 (val/test rollouts keep only digests + a redacted session); these tests now guard the fix."""
import json
import os

from conftest import add_skill, fake_log, fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, harness, tasks as tasks_mod

def _tasks():
    tr = [make_task("tr-%d" % i, "train", answer="== QA-TRAIN-%d ==\n" % i) for i in range(2)]
    va = [make_task("va-%d" % i, "val", answer="== QA-VAL-ANSWER-%d ==\n" % i) for i in range(2)]
    te = [make_task("te-%d" % i, "test", answer="== QA-TEST-ANSWER-%d ==\n" % i) for i in range(2)]
    return tr, va, te


def _files_containing(root, needle):
    hits = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                if needle in fh.read():
                    hits.append(os.path.relpath(p, root))
    return sorted(hits)


def test_qa_maintainer_and_proposer_can_reach_the_whole_workspace(tmp_path, answers):
    """The fact behind QA-F1 (passes today): maintainer/proposer rollouts use the workspace profile, their prompt
    names the workspace root and they have file + terminal tools - everything under <ws> is reachable for them."""
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=1, verbose=False)
    real_ws = os.path.realpath(ws)
    roles = [r for r in fake_log(tmp_path)
             if r["argv"][:1] == ["chat"] and os.path.realpath(r["env"]["HERMES_HOME"]).startswith(real_ws + os.sep)]
    assert len(roles) >= 2, "maintainer + proposer rollouts expected in the workspace profile"
    for r in roles:
        argv = r["argv"]
        query = open(argv[argv.index("--query-file") + 1], encoding="utf-8").read()
        tools = set(argv[argv.index("-t") + 1].split(",")) if "-t" in argv else set()
        assert "WORKSPACE: %s" % ws in query and {"file", "terminal"} <= tools, argv


def test_qa_val_answer_not_readable_after_gate(tmp_path, answers):
    tr, va, _te = _tasks()
    answers(tr + va)
    ws = fw_ws(tmp_path, tr + va)
    add_skill(ws)                                   # FAKE-SOLVE: val rollouts write the exact (sealed) answers
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    raw = open(os.path.join(ws, "tasks.json"), encoding="utf-8").read()
    assert "QA-VAL-ANSWER" not in raw                # the key itself is sealed (D19 fix) ...
    leaks = _files_containing(ws, "QA-VAL-ANSWER")
    assert not leaks, "val answers readable by the next maintainer/proposer: %s" % leaks   # ... its graded outputs are not


def test_qa_test_answer_not_readable_after_eval(tmp_path, answers):
    tr, va, te = _tasks()
    answers(tr + va + te)
    ws = fw_ws(tmp_path, tr + va)
    add_skill(ws)
    tf = tmp_path / "held" / "test.json"
    tf.parent.mkdir()
    tf.write_text(json.dumps(tasks_mod.seal_split(te)))   # sealed like the bench's test.json
    s = evaluate.evaluate(ws, str(tf), skills="active", repeat=1, tag="qa-exposure")
    assert s["mean"] == 1.0
    leaks = _files_containing(ws, "QA-TEST-ANSWER")
    assert not leaks, "held-out test answers readable by a later maintainer/proposer: %s" % leaks
