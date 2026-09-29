"""qa adversarial (D8, closes the WEAK row of QA-REVIEW-T2; QA-F7): run a REAL held-out eval first, then evolve - no
held-out text may be stored anywhere in the workspace (eval records keep digests only since core bc65da8), and no test
id, prompt, answer or eval path may reach the maintainer/proposer inputs, the wiki, proposals, traces or run records."""
import json
import os

from conftest import add_skill, fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, harness

CANARY = "QA-HELDOUT-CANARY"


def _test_tasks():
    out = []
    for i in range(2):
        t = make_task("qa-te-%d" % i, "test", answer="== %s-ANSWER-%d ==\n" % (CANARY, i))
        t["prompt"] = "Create report.txt for %s-PROMPT-%d in the house format." % (CANARY, i)
        t["title"] = "%s title %d" % (CANARY, i)
        out.append(t)
    return out


def test_qa_eval_outputs_never_reach_evolution_inputs(tmp_path, answers):
    ts, test = mini_tasks(), _test_tasks()
    answers(ts + test)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = tmp_path / "held-out-test.json"
    tf.write_text(json.dumps(test))
    s = evaluate.evaluate(ws, str(tf), skills="active", repeat=1, tag="qa-held")
    assert len(s["repeats"][0]["results"]) == 2
    eval_dir = os.path.join(ws, "eval", "qa-held")
    assert os.path.isdir(eval_dir)
    assert os.listdir(eval_dir)                                              # the eval really wrote its records ...
    assert not any(CANARY in open(os.path.join(dp, f), errors="replace").read()   # ... without held-out text (QA-F7)
                   for dp, _d, fs in os.walk(eval_dir) for f in fs), "held-out prompt/answer stored under <ws>/eval"
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)   # the skill solves everything: force the iteration
    assert os.path.isdir(os.path.join(ws, "runs", "maintain-01")) and os.path.isdir(os.path.join(ws, "runs", "propose-01"))
    leaks = []
    for dirpath, dirnames, files in os.walk(ws):
        rel = os.path.relpath(dirpath, ws)
        top = rel.split(os.sep)[0]
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.islink(p):
                continue
            text = open(p, errors="replace").read()
            if CANARY in text or ("qa-te-" in text and top != "eval"):   # ids may stay in eval records, text nowhere
                leaks.append(os.path.relpath(p, ws))
    assert not leaks, "held-out data outside <ws>/eval: %s" % leaks
    for tag in ("maintain-01", "propose-01"):
        for dirpath, _d, files in os.walk(os.path.join(ws, "runs", tag)):
            for f in files:
                assert "eval/qa-held" not in open(os.path.join(dirpath, f), errors="replace").read(), (tag, f)
