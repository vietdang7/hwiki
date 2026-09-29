"""T7 / QA-F7: held-out TEST data never informs the maintainer/proposer (SPEC R4).

(1) eval leaves no test task content (prompt, inputs, answers) under <ws>; (2) defence in depth:
a maintainer/proposer tool call/result touching <ws>/eval/** or a held-out task id makes the
iteration CONTAMINATED — proposal recorded invalid, no gate run, the iteration's wiki changes
discarded and purged."""
import json
import os
import subprocess

from conftest import add_skill, fake_log, fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, gating, harness, heldout, sessions, tasks as tasks_mod, wiki

CANARY = "CANARY-T7-HELDOUT"


def _test_tasks():
    out = []
    for i in range(2):
        t = make_task("te-%d" % i, "test", answer="== T7-ANSWER-%d ==\n" % i)
        t["prompt"] = "%s prompt %d: create report.txt for report id te-%d in the house format." % (CANARY, i, i)
        t["title"] = "%s title %d" % (CANARY, i)
        t["sandbox"] = {"data.csv": "id,note\nte-%d,%s-input\n" % (i, CANARY)}
        out.append(t)
    return out


def _files_containing(root, needle):
    hits = []
    for dirpath, dirnames, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p) and needle.encode() in open(p, "rb").read():
                hits.append(os.path.relpath(p, root))
    return sorted(hits)


def _evaluated_ws(tmp_path, answers, tag="t7"):
    ts, te = mini_tasks(), _test_tasks()
    answers(ts + te)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = tmp_path / "held" / "test.json"
    tf.parent.mkdir()
    tf.write_text(json.dumps(tasks_mod.seal_split(te)))
    s = evaluate.evaluate(ws, str(tf), skills="active", repeat=1, tag=tag)
    return ws, ts, s


# ---------------------------------------------------------------- (1) eval output
def test_f7_eval_leaves_no_test_task_content_in_ws(tmp_path, answers):
    ws, _ts, s = _evaluated_ws(tmp_path, answers)
    assert s["mean"] == 1.0 and [r["task_id"] for r in s["repeats"][0]["results"]] == ["te-0", "te-1"]
    for needle in (CANARY, "T7-ANSWER"):
        assert _files_containing(ws, needle) == [], needle   # incl. .git objects and the ws profile
    rdir = os.path.join(ws, "eval", "t7", "runs", "rep-1", "te-0")
    assert open(os.path.join(rdir, "query.txt")).read().startswith("<redacted: VAL/TEST prompt sha256:")
    sess = json.load(open(os.path.join(rdir, "session.jsonl")))
    user = [m for m in sess["messages"] if m["role"] == "user"][0]
    assert user["content"].startswith("sha256:")                      # the user prompt is hidden
    kept = json.load(open(os.path.join(rdir, "deliverables.json")))
    assert set(kept) == {"data.csv", "report.txt"} and all("sha256" in v for v in kept.values())


def test_f7_train_query_is_kept(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[0], 1)
    q = open(os.path.join(ws, "runs", "iter-01", "train", ts[0]["id"], "query.txt")).read()
    assert ts[0]["prompt"] in q


# ---------------------------------------------------------------- (2) detection
def _sess(tool, args, result):
    return {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "c", "tool_name": tool, "content": result}]}


def test_f7_detect_access_patterns(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "eval", "t", "runs", "rep-1", "te-orders-01"))
    ids = heldout.heldout_ids(ws)
    assert ids == {"te-orders-01"}
    flagged = [
        _sess("read_file", {"path": ws + "/eval/t/runs/rep-1/te-orders-01/query.txt"}, "x"),
        _sess("read_file", {"path": "eval/t/summary.json"}, "x"),                    # relative to the ws
        _sess("terminal", {"command": "ls eval"}, "t"),                               # the dir itself, typed
        _sess("terminal", {"command": "cat %s/../ws/eval/t/summary.json" % ws}, "x"),
        _sess("terminal", {"command": "find . -name '*.json'"}, "./eval/t/summary.json\n"),
        _sess("terminal", {"command": "grep -r orders ."}, "note about te-orders-01\n"),
    ]
    for s in flagged:
        hits = heldout.detect_access(s, ws, ids)
        assert hits and all("sha256:" in h for h in hits), s
    clean = [
        _sess("terminal", {"command": "ls %s" % ws}, "eval\nraw\nruns\nwiki\n"),     # a listing shows only the name
        _sess("read_file", {"path": ws + "/wiki/index.md"}, "evaluation notes; eval-free; te-orders-011"),
        _sess("skill_view", {"name": "wikiskill-maintainer"}, "never open %s/eval/ or te-orders-01" % ws),
    ]
    for s in clean:
        assert heldout.detect_access(s, ws, ids) == [], s


# ---------------------------------------------------------------- (2) evolve policy
def _wiki_log(ws):
    return subprocess.run(["git", "-C", os.path.join(ws, "wiki"), "log", "--all", "-p"],
                          capture_output=True, text=True).stdout


def test_f7_contaminated_maintainer_invalidates_iteration(tmp_path, answers, monkeypatch):
    ws, ts, _s = _evaluated_ws(tmp_path, answers)
    target = os.path.join(ws, "eval", "t7", "summary.json")
    monkeypatch.setenv("FAKE_HERMES_ROLE_READ", "maintainer:" + target)

    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and not h["accepted"]
    assert "contaminated: read eval data" in h["proposal"] and "maintainer" in h["proposal"]
    assert not os.path.exists(os.path.join(ws, "runs", "iter-01", "val"))           # no gate run
    chats = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]]
    assert chats  # sanity
    assert not os.path.exists(os.path.join(ws, "wiki", "patterns", "fake-pattern.md"))  # maintainer update dropped
    assert "fake pattern" not in _wiki_log(ws)                                          # ... and purged
    msess = json.load(open(os.path.join(ws, "runs", "maintain-01", "session.jsonl")))
    assert msess["redacted"] is True and "te-0" not in json.dumps(msess)
    pp = harness.proposal_path(ws, 1)  # T9: a contaminated maintainer skips the proposer
    assert not os.path.exists(pp) or json.load(open(pp))["action"] == "invalid"
    assert state["r_best"] == state["baseline"]


def test_f7_contaminated_proposer_invalidates_but_keeps_clean_wiki(tmp_path, answers, monkeypatch):
    ws, ts, _s = _evaluated_ws(tmp_path, answers)
    monkeypatch.setenv("FAKE_HERMES_ROLE_READ", "proposer:" + os.path.join(ws, "eval", "t7", "summary.json"))
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and "proposer" in h["proposal"] and "maintainer" not in h["proposal"]
    assert os.path.exists(os.path.join(ws, "wiki", "patterns", "fake-pattern.md"))    # clean maintainer kept
    assert not os.path.exists(os.path.join(ws, "runs", "iter-01", "val"))


def test_f7_clean_iteration_after_eval_still_gates(tmp_path, answers):
    ws, ts, _s = _evaluated_ws(tmp_path, answers)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert state["history"][-1]["r_val"] is not None
    assert "contaminated" not in json.dumps(state["history"])
