"""T5: qa findings QA-F1..F5 (reports/QA-REVIEW-T2.md) — core's own regression tests.

F1 graded VAL/TEST deliverables never reach the workspace (digests + redacted session only)
F2 escape detection scans tool RESULTS, resolves relative paths, HOME = rollout home
F3 hermes children get an allowlisted environment
F4 aborted rollouts leave nothing behind (signal cleanup + stale sweep)
F5 demote undoes only the promoted skills' `.usage.json` counters"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import add_skill, fake_log, fw_ws, make_task, mini_tasks
from hermes_wikiskill import evaluate, gating, harness, promote as pm, rollout, sessions, tasks as tasks_mod, traces
from hermes_wikiskill.backends import hermes


def _files_containing(root, needle):
    hits = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p) and needle in open(p, encoding="utf-8", errors="replace").read():
                hits.append(os.path.relpath(p, root))
    return hits


def _session(command, result, tool="terminal"):
    return {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": tool, "arguments": json.dumps({"command": command})}}]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": tool, "content": json.dumps({"output": result})}]}


# ---------------------------------------------------------------- F1
def test_f1_redact_keeps_receipts_and_digests_everything_else():
    s = {"id": "s1", "model": "m", "system_prompt": "<available_skills>\n    - house-report: x\n</available_skills>",
         "estimated_cost_usd": 0.01, "input_tokens": 5, "title": "== ANSWER ==",
         "messages": [
             {"role": "user", "content": "make report"},
             {"role": "assistant", "content": "", "reasoning": "== ANSWER ==", "tool_calls": [
                 {"id": "a", "type": "function", "function": {"name": "skill_view", "arguments": '{"name": "house-report"}'}},
                 {"id": "b", "type": "function", "function": {"name": "write_file",
                                                              "arguments": '{"path": "report.txt", "content": "== ANSWER =="}'}}]},
             {"role": "tool", "tool_call_id": "b", "tool_name": "write_file", "content": "wrote == ANSWER =="},
             {"role": "assistant", "content": "done: == ANSWER =="}]}
    r = sessions.redact(s)
    assert "ANSWER" not in json.dumps(r) and r["redacted"] is True
    assert r["system_prompt"] == s["system_prompt"] and r["estimated_cost_usd"] == 0.01 and r["input_tokens"] == 5
    assert sessions.skill_views(r) == ["house-report"]
    assert sessions.available_skills(r["system_prompt"]) == {"house-report"}
    assert [n for n, _ in sessions.tool_calls(r)] == ["skill_view", "write_file"]
    body = "done: == ANSWER =="
    assert r["messages"][3]["content"] == "sha256:%s len=%d" % (hashlib.sha256(body.encode()).hexdigest(), len(body))
    assert [m["role"] for m in r["messages"]] == ["user", "assistant", "tool", "assistant"]


def test_f1_val_answers_never_in_ws_train_artifacts_complete(tmp_path, answers):
    tr = [make_task("tr-%d" % i, "train", answer="== T5-TRAIN-%d ==\n" % i) for i in range(2)]
    va = [make_task("va-%d" % i, "val", answer="== T5-VAL-%d ==\n" % i) for i in range(2)]
    answers(tr + va)
    ws = fw_ws(tmp_path, tr + va)
    add_skill(ws)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert _files_containing(ws, "T5-VAL-") == []
    vdir = os.path.join(ws, "runs", "iter-00", "val", "va-0")
    run = json.load(open(os.path.join(vdir, "run.json")))
    assert run["redacted"] is True and run["score"] == 1.0 and run["skill_views"] == ["house-report"]
    assert json.load(open(os.path.join(vdir, "session.jsonl")))["redacted"] is True
    assert set(json.load(open(os.path.join(vdir, "deliverables.json")))) == {"data.csv", "report.txt"}
    meta = traces.load_trace(ws, 0, "val", "va-0")
    assert meta["skill_views"] == ["house-report"] and meta["score"] == 1.0
    tdir = os.path.join(ws, "runs", "iter-01", "train", "tr-0")
    assert open(os.path.join(tdir, "sandbox", "report.txt")).read() == "== T5-TRAIN-0 ==\n"
    assert not os.path.exists(os.path.join(tdir, "deliverables.json"))
    assert "redacted" not in json.load(open(os.path.join(tdir, "session.jsonl")))


def test_f1_eval_on_sealed_test_keeps_scores_not_answers(tmp_path, answers):
    te = [make_task("te-%d" % i, "test", answer="== T5-TEST-%d ==\n" % i) for i in range(2)]
    ts = mini_tasks()
    answers(ts + te)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    tf = tmp_path / "test.json"
    tf.write_text(json.dumps(tasks_mod.seal_split(te)))
    s = evaluate.evaluate(ws, str(tf), skills="active", repeat=1, tag="t5")
    assert s["mean"] == 1.0 and s["repeats"][0]["results"][0]["skill_views"] == ["house-report"]
    assert _files_containing(ws, "T5-TEST-") == []
    assert os.path.isfile(os.path.join(ws, "eval", "t5", "runs", "rep-1", "te-0", "deliverables.json"))


# ---------------------------------------------------------------- F2
def test_f2_tool_results_are_scanned(tmp_path):
    ws = str(tmp_path / "ws")
    seen = "%s/runs/iter-01/val/va-0/sandbox/report.txt\n== VAL ==\n" % ws
    hits = rollout.detect_escapes(_session("find / -name report.txt -exec cat {} +", seen), ws)
    assert hits and "result" in hits[0] and "workspace path" in hits[0]
    assert rollout.detect_escapes(_session("ls", "/home/ubuntu/ops/other/x\n"), ws)
    # QA-F6: results count only as PATH evidence — bare marker words there are not an escape
    assert rollout.detect_escapes(_session("ls", "/srv/x/tasks.json\n"), ws) == []
    assert rollout.detect_escapes(_session("pip list", "hermes-wikiskill 0.2.0\n"), ws) == []
    assert rollout.detect_escapes(_session("cat tasks.json", "x"), ws)          # typed marker still counts


def test_f2_relative_paths_resolved_against_sandbox(tmp_path):
    root = tmp_path / "rollouts"
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    me = root / "r-me"
    sandbox = me / "sandbox"
    sandbox.mkdir(parents=True)
    kw = {"sandbox": str(sandbox), "rollout_dir": str(me)}
    rel_ws = os.path.relpath(ws / "sub", sandbox)
    assert "workspace path" in rollout.detect_escapes(_session("cat %s/x" % rel_ws, ""), str(ws), **kw)[0]
    assert "other rollout" in rollout.detect_escapes(_session("cat ../../r-other/sandbox/report.txt", ""),
                                                     str(ws), **kw)[0]
    assert "other rollout" in rollout.detect_escapes(_session("ls", "%s/r-zz/home/.env" % root), str(ws), **kw)[0]
    ok = _session("cat ../sandbox/data.csv %s/report.txt" % sandbox, "%s/report.txt written" % sandbox)
    assert rollout.detect_escapes(ok, str(ws), **kw) == []


def test_f2_own_rollout_paths_never_flagged_whatever_the_root(tmp_path):
    me = tmp_path / "hermes-wikiskill-rollouts" / "r-me"   # a root whose name contains a marker
    (me / "sandbox").mkdir(parents=True)
    s = _session("cat %s/sandbox/data.csv" % me, "%s/sandbox/data.csv: ok" % me)
    assert rollout.detect_escapes(s, str(tmp_path / "ws"), sandbox=str(me / "sandbox"), rollout_dir=str(me)) == []


def test_f2_sealed_escape_record_carries_no_excerpt(tmp_path):
    ws = str(tmp_path / "ws")
    hits = rollout.detect_escapes(_session("cat %s/x" % ws, "== SECRET-ANSWER =="), ws, excerpts=False)
    assert hits and all("SECRET-ANSWER" not in h and "sha256:" in h for h in hits)


def test_f2_task_rollout_home_is_the_rollout_home(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[2], 1)
    chat = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]][-1]
    assert chat["home_env"] == chat["env"]["HERMES_HOME"] and chat["home_env"] != os.environ.get("HOME")
    assert os.path.realpath(chat["home_env"]).startswith(os.path.realpath(str(tmp_path / "rollouts")))


# ---------------------------------------------------------------- F3
def test_f3_hermes_children_get_an_allowlisted_env(tmp_path, answers, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "fake-token")
    monkeypatch.setenv("LC_ALL", "C")
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[2], 1)
    chats = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]]
    assert chats
    for r in chats:
        names = set(r["env_names"])
        assert "MY_CUSTOM_TOKEN" not in names and "HWS_HOME" not in names
        assert {"PATH", "LC_ALL", "HERMES_HOME", "DEEPSEEK_API_KEY"} <= names


def test_f3_home_env_pass_patterns_and_dotenv(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / ".env").write_text("DEEPSEEK_API_KEY=fake-key\n")
    monkeypatch.setenv("HWS_PASS_ENV", "FAKE_HERMES_*,EXACT_ONE")
    monkeypatch.setenv("EXACT_ONE", "1")
    monkeypatch.setenv("EXACT_ONE_MORE", "1")
    monkeypatch.setenv("OTHER_SECRET_TOKEN", "x")
    env = hermes.home_env(str(home))
    assert env["DEEPSEEK_API_KEY"] == "fake-key" and env["HERMES_HOME"] == str(home)
    assert "EXACT_ONE" in env and "EXACT_ONE_MORE" not in env and "OTHER_SECRET_TOKEN" not in env
    assert "FAKE_HERMES_LOG" in env and env["HOME"] == os.environ["HOME"]
    assert hermes.home_env(str(home), isolated=True)["HOME"] == str(home)


# ---------------------------------------------------------------- F4
def test_f4_sweep_removes_dead_owner_and_old_legacy_dirs_only(tmp_path):
    root = tmp_path / "rollouts"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for name, owner in (("r-dead", dead.pid), ("r-live", os.getpid())):
        (root / name / "home").mkdir(parents=True)
        (root / name / "home" / ".env").write_text("DEEPSEEK_API_KEY=fake-key\n")
        (root / name / rollout.OWNER_FILE).write_text(json.dumps({"pid": owner}))
    (root / "r-legacy-old").mkdir()
    (root / "r-legacy-new").mkdir()
    old = time.time() - rollout.LEGACY_STALE_S - 60
    os.utime(root / "r-legacy-old", (old, old))
    (root / "keep-me").mkdir()
    removed = {os.path.basename(p) for p in rollout.sweep_stale(str(root))}
    assert removed == {"r-dead", "r-legacy-old"}
    assert sorted(os.listdir(root)) == ["keep-me", "r-legacy-new", "r-live"]


def test_f4_sigterm_mid_rollout_removes_the_rollout_dir(tmp_path):
    ws = fw_ws(tmp_path)
    t = tasks_mod.load(ws)[2]
    seen = {}

    def runner(ws_, prompt, **kw):
        seen["home"] = kw["home"]
        assert os.path.isfile(os.path.join(os.path.dirname(kw["home"]), rollout.OWNER_FILE))
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("SIGTERM was not turned into SystemExit")

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as e:
        rollout.run_task_rollout(ws, t, run_dir=os.path.join(ws, "runs", "x"), skill_dirs=[], runner=runner)
    assert e.value.code == 128 + signal.SIGTERM
    assert not os.path.exists(os.path.dirname(seen["home"]))
    assert not os.path.exists(str(tmp_path / "rollouts"))
    assert signal.getsignal(signal.SIGTERM) == before


# ---------------------------------------------------------------- F5
@pytest.fixture
def accepted_ws(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=1, verbose=False)
    return ws


def _profile(tmp_path, usage=None):
    d = tmp_path / "hermes-root" / "profiles" / "hws-test-t5"
    (d / "skills" / "existing").mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n")
    (d / "skills" / "existing" / "SKILL.md").write_text("---\nname: existing\n---\nkeep\n")
    if usage is not None:
        (d / "skills" / ".usage.json").write_text(usage)
    return d


def test_f5_demote_keeps_other_skills_counters(tmp_path, accepted_ws):
    d = _profile(tmp_path, '{"existing": {"view_count": 1}}')
    pm.promote(accepted_ws, "hws-test-t5")
    (d / "skills" / ".usage.json").write_text(json.dumps(
        {"existing": {"view_count": 2}, "house-report": {"view_count": 3}}))
    (d / "skills" / ".usage.json.lock").write_text("")   # Hermes' own lock file
    out = pm.demote("hws-test-t5", last=True)
    assert json.load(open(d / "skills" / ".usage.json")) == {"existing": {"view_count": 2}}
    assert out["usage"]["action"] == "merged" and out["usage"]["removed"] == ["house-report"]
    assert out["diffs"] == [".usage.json"] and out["ignored"] == [".usage.json.lock"]
    assert not (d / "skills" / "house-report").exists()


def test_f5_demote_byte_identical_when_only_promoted_skill_was_used(tmp_path, accepted_ws):
    raw = '{ "existing" : {"view_count": 1} }'   # Hermes' own bytes, not our formatting
    d = _profile(tmp_path, raw)
    before = pm.manifest(str(d / "skills"))
    pm.promote(accepted_ws, "hws-test-t5")
    (d / "skills" / ".usage.json").write_text(json.dumps(
        {"existing": {"view_count": 1}, "house-report": {"view_count": 1}}))
    out = pm.demote("hws-test-t5", last=True)
    assert out["identical"] and out["usage"]["action"] == "restored"
    assert (d / "skills" / ".usage.json").read_text() == raw and pm.manifest(str(d / "skills")) == before


def test_f5_merge_usage_leaves_unparseable_file_alone(tmp_path):
    p = tmp_path / ".usage.json"
    p.write_text("{broken")
    assert pm.merge_usage(str(p), b"{}", ["x"])["action"] == "left" and p.read_text() == "{broken"


# ---------------------------------------------------------------- F6 (T6 regression from T5)
SKILL_TEXT = ("FAKE-SOLVE rules. Built by hermes-wikiskill from ~/ops/hermes-wikiskill/workspaces/x; "
              "never read tasks.json or test.json; see /home/ubuntu/ops/hermes-wikiskill/runs.")


def test_f6_skill_tool_results_are_never_escapes(tmp_path):
    ws = str(tmp_path / "ws")
    for tool in ("skill_view", "skills_list"):
        s = _session("x", SKILL_TEXT + " " + ws, tool=tool)
        s["messages"][0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"name": "house-report"})
        assert rollout.detect_escapes(s, ws) == [], tool
    s = _session("x", "ok", tool="skill_view")                      # a PATH typed into skill_view still counts
    s["messages"][0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"name": "x", "file_path": ws + "/t"})
    assert rollout.detect_escapes(s, ws)


def test_f6_skill_mentioning_the_framework_scores_normally(tmp_path, answers, monkeypatch):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws, body=SKILL_TEXT)
    r = gating.run_task(ws, ts[2], 1)
    assert r["score"] == 1.0 and r["escapes"] == [] and r["result"]["skill_views"] == ["house-report"]
    sess = json.load(open(os.path.join(ws, "runs", "iter-01", "val", ts[2]["id"], "session.jsonl")))
    assert sess["redacted"] is True  # the fake returned the skill text; the ws copy is still a digest
    monkeypatch.setenv("FAKE_HERMES_CHEAT", os.path.join(ws, "tasks.json"))
    r2 = gating.run_task(ws, ts[3], 1)
    assert r2["score"] == 0.0 and r2["escapes"], "a real escape must still be flagged"
