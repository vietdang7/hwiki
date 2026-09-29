"""T9 / ORCH Round 1: held-out isolation MAJORs (R1-01/02/03) + isolation MINORs.

R1-01  every role session is audited right after it ends (also when it raised), before anything
       is committed; an aborted run's wiki edits never survive a resume; attempts are kept and
       retro-audited; the manual maintain/propose verbs run the same audit.
R1-02  a role never lands edits in skills/active (contaminated OR clean iteration).
R1-03  path-typed args ('/', '..', '.'), $PWD idioms and non-skill tool RESULTS count as
       out-of-workspace access."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import add_skill, fw_ws, hws, mini_tasks
from hermes_wikiskill import gating, harness, heldout, rollout, transfer
from hermes_wikiskill.backends import hermes
from hermes_wikiskill.errors import HwsError, RolloutError, SkillsInvisibleError, UnsafePathError
from test_t8_confinement import CANARY, _calls_session, _canary_hits, outside  # noqa: F401 (fixture)

LEAK = "wiki/patterns/leak.md"


def _ws(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    return ws


def _role_calls(monkeypatch, role, calls):
    monkeypatch.setenv("FAKE_HERMES_ROLE_CALLS", json.dumps([[role, t, a] for t, a in calls]))


def _clear(monkeypatch):
    for k in ("FAKE_HERMES_ROLE_CALLS", "FAKE_HERMES_ROLE_EXIT"):
        monkeypatch.delenv(k, raising=False)


def _git_all(ws, sub, *args):
    return subprocess.run(["git", *args], cwd=os.path.join(ws, sub), capture_output=True, text=True).stdout


def _read_and_leak(out):
    """Read held-out-looking data outside the ws, then write it into the wiki."""
    return [("read_file", {"path": out + "/test.json"}),
            ("write_file", {"path": LEAK, "content": "# leak\n%s held-out prompt\n" % CANARY})]


# ------------------------------------------------------------------------------------ R1-01
def test_contaminated_maintainer_then_proposer_failure_is_laundered(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", _read_and_leak(outside))
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "proposer:130")
    try:
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    except HwsError:
        pass  # the unfixed code aborts in the proposer, never auditing the maintainer
    _clear(monkeypatch)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)  # resume
    h = state["history"][0]
    assert h["iter"] == 1 and h["r_val"] is None and not h["accepted"], h
    assert "contaminated" in h["proposal"] and "maintainer" in h["proposal"]
    assert _canary_hits(ws) == []                                   # not in any file / git object
    assert not os.path.exists(os.path.join(ws, LEAK))
    verdict = heldout.retro_audit(ws)["iterations"][0]
    assert verdict["verdict"] != "clean"


def test_interrupted_maintainer_edits_committed_on_resume(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", _read_and_leak(outside))
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert not os.path.exists(os.path.join(ws, LEAK))              # discarded on the raise path
    _clear(monkeypatch)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)  # clean resume
    h = state["history"][0]
    assert h["r_val"] is not None and "contaminated" not in h["proposal"]  # a clean, gated re-run
    assert _canary_hits(ws) == []
    assert LEAK not in _git_all(ws, "wiki", "log", "--all", "--name-only", "--format=")
    # the aborted attempt is kept (not overwritten) and retro-audited
    mdir = os.path.join(ws, "runs", "maintain-01")
    assert os.path.isfile(os.path.join(mdir, "attempt-1", "session.jsonl"))
    assert os.path.isfile(os.path.join(mdir, "session.jsonl"))
    it = heldout.retro_audit(ws)["iterations"][0]
    assert "maintainer/attempt-1" in it["roles"] and it["verdict"] != "clean"


def test_hard_killed_maintainer_edits_discarded_on_resume(tmp_path, answers, monkeypatch):
    """SIGKILL/OOM: no Python cleanup ran — the next evolve resets the wiki to the last audited commit."""
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    p = os.path.join(ws, LEAK)
    with open(p, "w") as f:
        f.write("# leak\n%s\n" % CANARY)
    with open(os.path.join(ws, "wiki", "index.md"), "a") as f:
        f.write("\n%s edited by a killed maintainer\n" % CANARY)
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []
    assert gating.load_state(ws)["wiki_audited"] == _git_all(ws, "wiki", "rev-parse", "HEAD").strip()


def test_manual_maintain_and_propose_verbs_are_audited(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", _read_and_leak(outside))
    r = hws("maintain", ws, "--iter", "1", "--json")
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert not os.path.exists(os.path.join(ws, LEAK)) and _canary_hits(ws) == []

    _role_calls(monkeypatch, "proposer", [("read_file", {"path": outside + "/test.json"})])
    r = hws("propose", ws, "--iter", "1", "--json")
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert json.load(open(harness.proposal_path(ws, 1)))["action"] == "invalid"
    assert "contaminated" in json.loads(r.stdout)["invalid"]

    _clear(monkeypatch)  # a clean manual maintain commits and records the audited wiki head
    r = hws("maintain", ws, "--iter", "1", "--json")
    assert r.returncode == 0, r.stderr
    assert gating.load_state(ws)["wiki_audited"] == _git_all(ws, "wiki", "rev-parse", "HEAD").strip()
    assert os.path.exists(os.path.join(ws, "wiki", "patterns", "fake-pattern.md"))


# ------------------------------------------------------------------------------------ R1-02
def _active_log(ws):
    return _git_all(ws, "skills/active", "log", "--all", "--name-only", "--format=%s")


def test_contaminated_proposer_direct_skill_write_survives(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "proposer", [
        ("read_file", {"path": outside + "/test.json"}),
        ("write_file", {"path": "skills/active/leaked/SKILL.md", "content":
                        "---\nname: leaked\ndescription: x\n---\n%s\n" % CANARY})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and "contaminated" in h["proposal"]
    assert [os.path.basename(d) for d in hermes.active_skill_dirs(ws)] == ["house-report"]
    assert "leaked" not in _active_log(ws)
    assert _canary_hits(ws) == []


@pytest.mark.parametrize("proposal", ["create", "none"])
def test_clean_role_direct_skill_write_is_invalid(tmp_path, answers, monkeypatch, proposal):
    ws = _ws(tmp_path, answers)
    if proposal == "none":
        monkeypatch.setenv("FAKE_HERMES_PROPOSAL", "none")  # would be a no_action iteration
    _role_calls(monkeypatch, "proposer", [("write_file", {"path": "skills/active/sneaky/SKILL.md", "content":
                                                          "---\nname: sneaky\ndescription: x\n---\nungated\n"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and not h["accepted"]
    assert "role wrote skills/active directly" in h["proposal"]
    assert not os.path.exists(os.path.join(ws, "skills", "active", "sneaky"))
    assert "sneaky" not in _active_log(ws)
    assert not gating.active_dirty(ws)


def test_commit_base_refuses_dirty_active(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    add_skill(ws, "extra", commit=False)
    with pytest.raises(ValueError):
        gating.commit_base(ws, 1)
    gating.reset_active(ws)
    assert not gating.active_dirty(ws) and not os.path.exists(os.path.join(ws, "skills", "active", "extra"))


# ------------------------------------------------------------------------------------ R1-03
@pytest.mark.parametrize("path", ["/", ".."])
def test_search_files_root_or_parent_not_flagged(tmp_path, answers, monkeypatch, path):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", [("search_files", {"pattern": "held-out", "path": path})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert not h["accepted"] and h["r_val"] is None
    assert "out-of-workspace access" in h["proposal"]


PARENT_IDIOMS = {
    "read-dotdot": ("read_file", {"path": ".."}),
    "ls-pwd-parent": ("terminal", {"command": "ls $PWD/.."}),
    "find-dirname-pwd": ("terminal", {"command": 'find "$(dirname "$PWD")" -name test.json'}),
    "oldpwd": ("terminal", {"command": "cd wiki && ls $OLDPWD/.."}),
    "list-root-dir": ("list_directory", {"dir": "/"}),
}


@pytest.mark.parametrize("case", sorted(PARENT_IDIOMS))
def test_parent_and_root_idioms_flagged(tmp_path, case):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    assert heldout.detect_outside(_calls_session([PARENT_IDIOMS[case]]), ws), case


def _with_results(calls_results):
    calls = [{"id": str(i), "type": "function", "function": {"name": t, "arguments": json.dumps(a)}}
             for i, (t, a, _r) in enumerate(calls_results)]
    msgs = [{"role": "assistant", "content": "", "tool_calls": calls}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "tool_name": t, "content": r}
             for i, (t, _a, r) in enumerate(calls_results)]
    return {"messages": msgs}


def test_results_with_outside_paths_flagged(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    bad = _with_results([("terminal", {"command": "ls"},
                          json.dumps({"output": "/home/u/ops/hermes-wikiskill-src/benches/house-report/test.json\n"}))])
    assert heldout.detect_outside(bad, ws)


NEGATIVES = [
    ("terminal", {"command": "pwd"}, json.dumps({"output": "WS\n"})),
    ("terminal", {"command": "python3 x.py"}, json.dumps({"output": 'Traceback:\n  File "/usr/lib/python3.12/json/__init__.py", line 1\n'})),
    ("search_files", {"pattern": "FIX", "path": "."}, json.dumps({"matches": ["wiki/patterns/p.md:1: FIX"]})),
    ("search_files", {"pattern": "FIX", "path": "wiki", "target": "content"}, json.dumps({"matches": ["patterns/p.md"]})),
    ("skill_view", {"name": "x"}, json.dumps({"content": "see /home/u/ops/whatever"})),
    # host eff-house-report iter 1: a trace-analysis script quoting a train rollout's `grep ... /tmp/)`
    ("terminal", {"command": "python3 .hermes-home/cache/scratch/analyze.py | head"},
     json.dumps({"output": "terminal(grep -a -o x /tmp/hws-rollouts/r-1/home/state.db /tmp/)\n"})),
]


def test_negatives_not_flagged(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    calls = [(t, a, r.replace("WS", ws)) for t, a, r in NEGATIVES]
    assert heldout.detect_outside(_with_results(calls), ws) == []


# ------------------------------------------------------------------------------------ minors
def test_r1_12_skills_invisible_names_the_ws_run_dir(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    monkeypatch.setenv("FAKE_HERMES_HIDE_SKILLS", "1")
    with pytest.raises(SkillsInvisibleError) as e:
        gating.run_task(ws, mini_tasks()[0], 1, overwrite=True)
    msg = str(e.value)
    assert os.path.join(ws, "runs") in msg and rollout.rollout_root(ws) not in msg


def test_r1_16_symlinked_skill_is_refused(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    d = add_skill(ws, "linky", commit=False)
    os.symlink("/etc/hosts", os.path.join(d, "hosts"))
    with pytest.raises(UnsafePathError):
        hermes.copy_skill_dir(d, str(tmp_path / "copy"))
    assert not os.path.exists(tmp_path / "copy" / "hosts")
    dst = fw_ws(tmp_path, mini_tasks(), name="dst")
    transfer.transfer(ws, dst) if hasattr(transfer, "transfer") else None
    assert not os.path.lexists(os.path.join(dst, "skills", "active", "linky", "hosts"))


def test_r1_17_no_provider_key_left_in_ws(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    envs = [os.path.join(d, f) for d, _, fs in os.walk(ws) for f in fs if f == ".env"]
    assert envs == []
    assert not os.path.exists(os.path.join(ws, ".hws-roles"))


def test_r1_18_sigterm_kills_role_child_and_removes_role_home(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    pidfile = str(tmp_path / "hermes.pid")
    code = ("import sys; from hermes_wikiskill.backends import hermes\n"
            "hermes.run(sys.argv[1], 'You are the Wiki Maintainer. WORKSPACE: ' + sys.argv[1],"
            " run_dir=sys.argv[1] + '/runs/maintain-09', role=True)\n")
    env = {**os.environ, "FAKE_HERMES_SLEEP": "30", "FAKE_HERMES_PIDFILE": pidfile}
    p = subprocess.Popen([sys.executable, "-c", code, ws], env=env)
    for _ in range(200):
        if os.path.exists(pidfile) and os.path.getsize(pidfile):
            break
        time.sleep(0.05)
    child = int(open(pidfile).read())
    assert [n for n in os.listdir(os.path.join(ws, ".hws-roles")) if n.startswith("r-")]
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 128 + signal.SIGTERM
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert not os.path.exists(os.path.join(ws, ".hws-roles"))


def test_r1_19_pass_env_needs_testing_mode(monkeypatch):
    monkeypatch.setenv("HWS_PASS_ENV", "*,FAKE_HERMES_*,MY_API_KEY,GH_TOKEN*,QA_X")
    assert hermes.pass_env_patterns() == ["FAKE_HERMES_*", "QA_X"]
    monkeypatch.delenv("HWS_TESTING")
    assert hermes.pass_env_patterns() == []


def test_r1_14_skills_listed_recorded(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    r = gating.run_task(ws, mini_tasks()[0], 1, overwrite=True)
    rec = json.load(open(os.path.join(ws, "runs", "iter-01", "train", "tr-0", "run.json")))
    assert rec["skills_listed"] == ["house-report"]
    assert r.get("skills_listed", ["house-report"]) == ["house-report"]


# ------------------------------------------------------------------------------------ QA-F10 (R1-02 addendum)
def _git_config(ws, repo):
    return open(os.path.join(ws, repo, ".git", "config")).read()


def _marker(ws, name):
    """Inside the ws: the role's path args must not trip the out-of-ws detector, so only the
    git-metadata defence is under test."""
    return os.path.join(ws, "scratch", "ran-" + name)


def test_f10_lying_fsmonitor_poc(tmp_path, answers, monkeypatch):
    """PoC: a role sets core.fsmonitor in skills/active/.git (a script that says "nothing changed")
    and edits a tracked SKILL.md. The framework's git must never run it, the edit must not survive."""
    ws = _ws(tmp_path, answers)
    mark = _marker(ws, "fsm")
    script = os.path.join(ws, "scratch", "fsm.sh")
    cfg = _git_config(ws, "skills/active") + '[core]\n\tfsmonitor = "sh %s"\n' % script
    _role_calls(monkeypatch, "proposer", [
        ("write_file", {"path": script, "content": '#!/bin/sh\ntouch %s\nprintf "tok\\0"\n' % mark}),
        ("write_file", {"path": "skills/active/.git/config", "content": cfg}),
        ("write_file", {"path": "skills/active/house-report/SKILL.md",
                        "content": "---\nname: house-report\ndescription: x\n---\nFAKE-SOLVE ungated edit\n"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert not os.path.exists(mark)                                  # framework git never ran the role's hook
    assert h["r_val"] is None and "role modified git metadata" in h["proposal"], h
    assert "fsmonitor" not in _git_config(ws, "skills/active")
    assert "ungated edit" not in open(os.path.join(ws, "skills", "active", "house-report", "SKILL.md")).read()
    assert not gating.active_dirty(ws)


@pytest.mark.parametrize("repo,role", [("skills/active", "proposer"), ("wiki", "maintainer")])
def test_f10_attributes_filter_driver(tmp_path, answers, monkeypatch, repo, role):
    ws = _ws(tmp_path, answers)
    mark = _marker(ws, "filter")
    cfg = _git_config(ws, repo) + "[filter \"evil\"]\n\tclean = sh -c 'touch %s; cat'\n" % mark
    _role_calls(monkeypatch, role, [
        ("write_file", {"path": repo + "/.git/info/attributes", "content": "* filter=evil\n"}),
        ("write_file", {"path": repo + "/.git/config", "content": cfg})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert not os.path.exists(mark)
    assert h["r_val"] is None and "role modified git metadata" in h["proposal"], h
    assert not os.path.exists(os.path.join(ws, repo, ".git", "info", "attributes"))
    assert "evil" not in _git_config(ws, repo)


def test_f10_direct_git_commit_in_active(tmp_path, answers, monkeypatch):
    """A role's own `git commit` in skills/active (clean tree, HEAD moved) is not a tree change."""
    ws = _ws(tmp_path, answers)
    act = os.path.join(ws, "skills", "active")
    body = "---\nname: sneaky\ndescription: x\n---\nungated\n"
    env = {**os.environ, "GIT_AUTHOR_NAME": "r", "GIT_AUTHOR_EMAIL": "r@r", "GIT_COMMITTER_NAME": "r",
           "GIT_COMMITTER_EMAIL": "r@r", "GIT_INDEX_FILE": str(tmp_path / "role-index")}
    run = lambda *a: subprocess.run(["git", *a], cwd=act, env=env, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=act, input=body, text=True,
                          capture_output=True, check=True).stdout.strip()
    run("read-tree", "HEAD")
    run("update-index", "--add", "--cacheinfo", "100644,%s,sneaky/SKILL.md" % blob)
    commit = run("commit-tree", run("write-tree"), "-p", "HEAD", "-m", "role commit")
    ref = run("symbolic-ref", "HEAD")
    _role_calls(monkeypatch, "proposer", [
        ("write_file", {"path": "skills/active/.git/" + ref, "content": commit + "\n"}),
        ("write_file", {"path": "skills/active/sneaky/SKILL.md", "content": body})])
    head0 = gating.active_head(ws)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and "role modified git metadata" in h["proposal"], h
    assert not os.path.exists(os.path.join(act, "sneaky"))
    assert "sneaky" not in subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=act,
                                          capture_output=True, text=True).stdout
    assert commit not in subprocess.run(["git", "rev-list", "HEAD"], cwd=act, capture_output=True, text=True).stdout
    assert gating.active_head(ws) != commit and head0


def test_f10_resume_after_killed_role(tmp_path, answers):
    """Hard kill mid-role: no snapshot survives. HEAD moved → reset to the recorded head; tampered
    config → refuse to run git in that repo."""
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    act = os.path.join(ws, "skills", "active")
    head = gating.active_head(ws)
    st = gating.load_state(ws)
    st["role_running"] = {"role": "proposer", "active_head": head}
    gating.save_state(ws, st)
    add_skill(ws, "sneaky")                                           # the killed role's direct commit
    assert gating.active_head(ws) != head
    with pytest.raises(HwsError, match="no pre-role snapshot"):  # T10: legacy marker → fail closed once
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "sneaky" not in os.listdir(act) and "role_running" not in gating.load_state(ws)
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "sneaky" not in subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=act,
                                          capture_output=True, text=True).stdout
    with open(os.path.join(act, ".git", "config"), "a") as f:
        f.write('[core]\n\tfsmonitor = "sh /nonexistent"\n')
    with pytest.raises(HwsError, match="git metadata modified outside the harness"):
        harness.evolve(ws, iters=3, verbose=False, no_early_stop=True)


def test_f10_gitutil_ignores_env_redirection_and_fsmonitor(tmp_path, monkeypatch):
    from hermes_wikiskill import gitutil
    r = str(tmp_path / "r")
    os.makedirs(r)
    gitutil.init_repo(r, "x")
    mark = str(tmp_path / "ran")
    subprocess.run(["git", "config", "core.fsmonitor", "sh -c 'touch %s'" % mark], cwd=r, check=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert gitutil.is_clean(r) and not os.path.exists(mark)
    snap = gitutil.meta_snapshot(r)
    with open(os.path.join(r, ".git", "info", "attributes"), "w") as f:
        f.write("* filter=x\n")
    assert gitutil.meta_changed(r, snap) == ["info/attributes"]
    gitutil.meta_restore(r, snap)
    assert gitutil.meta_changed(r, snap) == []
    assert gitutil.unsafe_metadata(r) == ["config core.fsmonitor"]


# ------------------------------------------------------------------------------------ T9b: live AC11 false positives
# minimal strings from the official C4 run framework-20260928T191716Z (maintainer tap sessions dea87d / 656f56)
LIVE_PROSE = [("write_file", {"path": "wiki/patterns/sandbox.md",
                              "content": "expenses-01 listed and grepped\n    `/tmp/<rollout>/home` against the task's explicit rule\n"}),
              ("patch", {"path": "wiki/patterns/sandbox.md", "old_string": "x",
                         "new_string": "(d) listing/grepping /tmp/<rollout>/home outside the\n  working directory"})]
LIVE_RESULT = ("terminal", {"command": "python3 .hermes-home/cache/scratch/dump.py | head -50"},
               json.dumps({"output": 'ls -la skills memories cron hooks 2>/dev/null\\"}"}}]\n\n--- [10] '
                                     'find /tmp/hws-rollouts/r-1 -maxdepth 3 -type d 2>/dev/null\\"}"}}]\n'}))


def test_t9b_live_prose_and_escaped_devnull_not_flagged(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    calls = [(t, a, '{"success": true}') for t, a in LIVE_PROSE] + [LIVE_RESULT]
    assert heldout.detect_outside(_with_results(calls), ws) == []


@pytest.mark.parametrize("call", [("terminal", {"command": "ls /home"}),
                                  ("search_files", {"pattern": "x", "path": "/home"}),
                                  ("write_file", {"path": "wiki/p.md", "content": "see /home/u/ops/x/test.json\n"}),
                                  ("terminal", {"command": "cat /home/u/ops/x/test.json\\\n | head"})])
def test_t9b_real_accesses_still_flagged(tmp_path, call):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    assert heldout.detect_outside(_calls_session([call]), ws), call


def test_t9b_live_prose_maintainer_still_gates(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", [LIVE_PROSE[0]])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is not None and "invalid" not in h["proposal"], h


# ------------------------------------------------------------------------------------ T9c: own hermes binary in run records
def test_t9c_result_with_framework_hermes_binary_not_flagged(tmp_path, monkeypatch):
    """qa replay: a role cats runs/**/run.json, whose cmd[0] is the hermes binary the framework
    launched (outside the ws) — that RESULT is not an access. A TYPED path to it still is.
    Binary + link live next to the ws (siblings are never allowlisted), wherever tmp is."""
    import shutil
    from conftest import FAKE_HERMES
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "runs"))
    binary = str(tmp_path / "venv" / "bin" / "hermes")
    os.makedirs(os.path.dirname(binary))
    shutil.copy2(FAKE_HERMES, binary)
    link = str(tmp_path / "local-bin" / "hermes")  # ~/.local/bin/hermes -> <venv>/bin/hermes
    os.makedirs(os.path.dirname(link))
    os.symlink(binary, link)

    def cat_run_json(*cmd0):
        out = " ".join(json.dumps({"cmd": [c, "chat", "--query-file", "<query.txt>"], "exit_code": 0}) for c in cmd0)
        return _with_results([("terminal", {"command": "cat runs/iter-01/train/train-sales-01/run.json"},
                               json.dumps({"output": out}))])

    assert heldout.detect_outside(cat_run_json(binary), ws)            # not the launched binary yet
    monkeypatch.setenv("HERMES_BIN", binary)
    assert heldout.detect_outside(cat_run_json(binary), ws) == []
    monkeypatch.setenv("HERMES_BIN", link)                              # exact path AND realpath
    assert heldout.detect_outside(cat_run_json(link, binary), ws) == []
    assert heldout.detect_outside(_calls_session([("read_file", {"path": binary})]), ws)   # typed: still access
    assert heldout.detect_outside(_calls_session([("terminal", {"command": "cat " + link})]), ws)
    assert heldout.detect_outside(cat_run_json(str(tmp_path / "other" / "bin" / "hermes")), ws)


# ------------------------------------------------------------------------------------ T9d: diagnosable voids
def test_t9d_outside_paths_recorded_in_confinement_json_and_history(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    target = outside + "/test.json"
    _role_calls(monkeypatch, "maintainer", [("read_file", {"path": target}),
                                           ("terminal", {"command": "ls /home/u-t9d/ops"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    from hermes_wikiskill import sessions
    raw = open(os.path.join(ws, "runs", "maintain-01", "confinement.json")).read()
    conf = json.loads(raw)
    # T10g (QA-F27): the ws record is PATH-FREE (digest + tool.key + kind); readable paths are operator-only
    assert {"tool": "read_file", "key": "path", "kind": "arg", "digest": sessions.digest(target)} in conf["outside"]
    assert {"tool": "terminal", "key": "command", "kind": "arg",
            "digest": sessions.digest("/home/u-t9d/ops")} in conf["outside"]
    assert target not in raw and "/home/u-t9d" not in raw
    assert conf["counts"]["outside"] == 2 and conf["counts"]["eval"] == 0
    h = state["history"][-1]["proposal"]
    assert "out-of-workspace access" in h and sessions.digest(target) in h and "(read_file.path arg)" in h
    assert target not in h
    _tag, wsid, name = conf["diagnostic"].rsplit(" ", 2)
    diag = json.load(open(os.path.join(os.environ["HWS_HOME"], "diagnostics", wsid, name)))
    assert {"tool": "read_file", "key": "path", "kind": "arg", "path": target} in diag["outside"]
    assert _canary_hits(ws) == []                                      # paths only, never the content


def test_t9d_eval_hits_record_no_paths(tmp_path, answers, monkeypatch):
    from test_t7_heldout import _evaluated_ws
    ws, _ts, _s = _evaluated_ws(tmp_path, answers)
    monkeypatch.setenv("FAKE_HERMES_ROLE_READ", "maintainer:" + os.path.join(ws, "eval", "t7", "summary.json"))
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    conf = json.load(open(os.path.join(ws, "runs", "maintain-01", "confinement.json")))
    assert conf["outside"] == [] and conf["counts"]["eval"] >= 1
    assert "/eval/" not in state["history"][-1]["proposal"] and "te-0" not in json.dumps(conf)


def test_t9d_outside_paths_results_masking_and_length(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    long = "/home/u/ops/" + "d" * 200 + "/x.json"
    sess = _with_results([("terminal", {"command": "python3 x.py"},
                           json.dumps({"output": "/home/u/ops/bench/te-7/test.json\n"})),
                          ("read_file", {"path": long}, "{}")])
    paths = heldout.outside_paths(sess, ws, ids={"te-7"})
    assert {"tool": "terminal", "key": "result", "kind": "result",
            "path": "/home/u/ops/bench/<held-out-id>/test.json"} in paths
    assert all(len(d["path"]) <= 160 for d in paths)
    assert any(d["tool"] == "read_file" and d["path"].endswith("...") for d in paths)
    note = heldout.contamination_note({"maintainer": {"outside": ["x"], "outside_paths": paths}})
    from hermes_wikiskill import sessions  # T10g: the note carries the digest, never the path
    assert f"paths: {sessions.digest('/home/u/ops/bench/<held-out-id>/test.json')} (terminal.result result)" in note
    assert "/home/u/ops" not in note


# ------------------------------------------------------------------------------------ T9e: quoted rollout paths (QA-F12)
# qa diag of the voided final-build evolve (diag-20260928T204423Z, maintain --iter 3): 7/7 flagged paths were quotes
T9E_PROSE = [
    ("write_file", {"path": "wiki/patterns/escape.md",
                    "content": "# Escapes\nTrain agents probe sibling rollout dirs under /tmp/hws-rollouts/ and their own home.\n"}),
    ("patch", {"path": "wiki/patterns/escape.md", "old_string": "sibling rollout dirs under /tmp/hws-rollouts/",
               "new_string": "sibling rollout dirs under /tmp/hws-rollouts/r-*/sandbox"}),
    ("write_file", {"path": "wiki/traces/train-03.md",
                    "content": "The agent ran `ls` of `../home/` before reading data.csv; blocked.\n"}),
]
T9E_RESULT = ("terminal", {"command": "python3 .hws-roles/r-x/cache/scratch/show_traces.py | cut -c1-80"},
              json.dumps({"output": "train-03 tool=terminal cmd=ls -la /tmp/hws-rollout\ntrain-05 cd /tmp/hws-roll\n"
                                    "train-07 find /tmp/hws\n"}))


def test_t9e_quoted_rollout_paths_and_prose_relatives_not_flagged(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    calls = [(t, a, '{"success": true}') for t, a in T9E_PROSE] + [T9E_RESULT]
    assert heldout.detect_outside(_with_results(calls), ws) == []


# (relative escapes climb to a SIBLING of the ws: in pytest `../../` lands in the allowlisted tmp scratch)
@pytest.mark.parametrize("call", [
    ("terminal", {"command": "ls /tmp/hws-rollouts"}),
    ("search_files", {"pattern": "report", "path": "/tmp/hws-rollouts"}),
    ("read_file", {"path": "/tmp/hws-rollouts/r-abc/sandbox/answer.txt"}),
    ("write_file", {"path": "scratch/peek.py",
                    "content": "print(open('../hermes-wikiskill-src/benches/house-report/test.json').read())\n"}),
    ("write_file", {"path": "scratch/peek.md", "content": "#!/usr/bin/env python3\nopen('../other/test.json')\n"}),
    ("patch", {"path": "scratch/peek.sh", "old_string": "x", "new_string": "cat ../other-ws/tasks.json"}),
    ("read_file", {"path": "/home/ubuntu/ops/hermes-wikiskill-src/benches/house-report/test.json"}),
    ("write_file", {"path": "wiki/p.md", "content": "see /home/ubuntu/ops/hermes-wikiskill-src/benches/x/test.json\n"}),
])
def test_t9e_typed_access_still_flagged(tmp_path, call):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    assert heldout.detect_outside(_calls_session([call]), ws), call


def test_t9e_result_prefix_rule_is_only_for_rollout_roots(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    other = _with_results([("terminal", {"command": "python3 x.py"},
                            json.dumps({"output": "/home/ubuntu/ops/hermes-wikiskill-src\n"}))])
    assert heldout.detect_outside(other, ws)


def test_t9e_maintainer_documenting_escapes_still_gates(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", [T9E_PROSE[0], T9E_PROSE[2]])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is not None and "invalid" not in h["proposal"], h


# ------------------------------------------------------------------------------------ T9f: false voids in official C4 (7a3da1e)
# AC17 maintainer (framework-20260928T211205Z tap dd80b7): the word "eval" in wiki prose; its own `git commit` in the wiki
T9F_PROSE = [("write_file", {"path": "wiki/patterns/no-spec-chase-outside-sandbox.md",
                             "content": "# No spec chase\n...(the eval's own test fixtures were the only \"hits\")...\n"
                                        "Never open `eval/`; skip test/eval artifacts that must not be used.\n"}),
             ("patch", {"path": "wiki/patterns/no-spec-chase-outside-sandbox.md", "old_string": "x",
                        "new_string": "never open eval/ (the eval dir holds held-out runs)"})]


def _eval_ws(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "eval", "x"))
    os.makedirs(os.path.join(ws, "wiki"))
    return ws


def test_t9f_prose_mentions_of_eval_not_flagged(tmp_path):
    ws = _eval_ws(tmp_path)
    assert heldout.detect_access(_calls_session(T9F_PROSE), ws, {"te-0"}) == []


@pytest.mark.parametrize("case", ["search-eval", "ls-eval", "dotdot-eval", "abs-prose", "py-open", "id-prose", "read-eval"])
def test_t9f_real_eval_access_still_flagged(tmp_path, case):
    ws = _eval_ws(tmp_path)
    call = {"search-eval": ("search_files", {"pattern": "x", "path": "eval"}),
            "ls-eval": ("terminal", {"command": "ls eval"}),
            "dotdot-eval": ("terminal", {"command": "cd wiki && cat ../eval/x/summary.json"}),
            "abs-prose": ("write_file", {"path": "wiki/p.md", "content": "see %s/eval/x for scores" % ws}),
            "py-open": ("write_file", {"path": "scratch/a.py", "content": "print(open('eval/t/summary.json').read())\n"}),
            "id-prose": ("write_file", {"path": "wiki/p.md", "content": "task te-0 was hard"}),
            "read-eval": ("read_file", {"path": "eval"})}[case]
    assert heldout.detect_access(_calls_session([call]), ws, {"te-0"}), case


def _role_wiki_commit(ws, tmp_path, rel, body):
    """A commit object as the maintainer's own `git add -A && git commit` in wiki/ would make."""
    w = os.path.join(ws, "wiki")
    env = {**os.environ, "GIT_AUTHOR_NAME": "r", "GIT_AUTHOR_EMAIL": "r@r", "GIT_COMMITTER_NAME": "r",
           "GIT_COMMITTER_EMAIL": "r@r", "GIT_INDEX_FILE": str(tmp_path / "role-wiki-index")}
    run = lambda *a, **kw: subprocess.run(["git", *a], cwd=w, env=env, check=True, capture_output=True, text=True, **kw).stdout.strip()  # noqa: E731
    blob = run("hash-object", "-w", "--stdin", input=body)
    run("read-tree", "HEAD")
    run("update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, rel))
    commit = run("commit-tree", run("write-tree"), "-p", "HEAD", "-m", "iter-01: maintainer notes")
    return commit, run("symbolic-ref", "HEAD")


def test_t9f_maintainer_own_wiki_commit_undone_not_void(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    from hermes_wikiskill import wiki
    wiki.ensure(ws)
    body = "# Role note\nPROBLEM: format. FIX: house convention.\n"
    commit, ref = _role_wiki_commit(ws, tmp_path, "patterns/role-note.md", body)
    _role_calls(monkeypatch, "maintainer", [("write_file", {"path": "wiki/patterns/role-note.md", "content": body}),
                                            ("write_file", {"path": "wiki/.git/" + ref, "content": commit + "\n"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is not None and "invalid" not in h["proposal"], h           # gate ran
    assert any("own wiki commits undone" in n for n in h.get("role_notes", [])), h
    w = os.path.join(ws, "wiki")
    git = lambda *a: subprocess.run(["git", *a], cwd=w, capture_output=True, text=True).stdout  # noqa: E731
    assert "patterns/role-note.md" in git("ls-tree", "-r", "--name-only", "HEAD")    # framework commit has the edits
    assert commit not in git("rev-list", "HEAD")                                    # role commit unreachable from HEAD
    assert "iter-01: maintainer notes" not in git("log", "--format=%s", "HEAD")
    rec = json.load(open(os.path.join(ws, "runs", "maintain-01", "run.json")))
    assert any("own wiki commits undone" in n for n in rec.get("git_notes", []))
    assert "own wiki commits undone" in open(os.path.join(w, "log.md")).read()


def test_t9f_wiki_head_change_still_invalid(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", [("write_file", {"path": "wiki/.git/HEAD", "content": "ref: refs/heads/other\n"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and "role modified git metadata" in h["proposal"], h


# ------------------------------------------------------------------------------------ T9g: F7 only with held-out data present
# official C4 on b81894b (framework-20260928T214937Z, AC11 clean ws, no <ws>/eval): maintainer 6356fe `find <ws> -name eval`,
# proposer a301a5 self-check .py with the literal "eval/" -> both iterations voided
def _probe_calls(ws):
    return [("terminal", {"command": "find %s -maxdepth 2 -name eval -o -maxdepth 2 -name 'expected*' | head" % ws}),
            ("terminal", {"command": "ls eval"}),
            ("write_file", {"path": "scratch/verify.py", "content":
                            'sm = open("skills/active/house-report/SKILL.md").read()\n'
                            'print("eval/val mentions in skill:", [w for w in ["eval/", "expected_sha"] if w in sm])\n'}),
            ("terminal", {"command": "cat %s/eval/x/summary.json" % ws})]


def _put_eval_data(ws, tag="later"):
    d = os.path.join(ws, "eval", tag)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "summary.json"), "w") as f:
        f.write("{}")


def test_t9g_no_heldout_data_no_f7_void(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    assert not os.path.exists(os.path.join(ws, "eval"))                          # a CLEAN ws (AC11)
    _role_calls(monkeypatch, "maintainer", _probe_calls(ws))
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is not None and "contaminated" not in h["proposal"], h      # gate ran
    rec = json.load(open(os.path.join(ws, "runs", "maintain-01", "run.json")))
    assert rec["heldout_present"] is False
    assert json.load(open(os.path.join(ws, "runs", "propose-01", "run.json")))["heldout_present"] is False
    # eval data created LATER: the recorded flag keeps the retro verdict clean ...
    _put_eval_data(ws)
    it = heldout.retro_audit(ws)["iterations"][0]
    assert it["verdict"] == "clean" and it["roles"]["maintainer"]["heldout_present"] is False, it
    # ... while a legacy record (no field) with eval present now is judged as before
    p = os.path.join(ws, "runs", "maintain-01", "run.json")
    rec.pop("heldout_present")
    with open(p, "w") as f:
        json.dump(rec, f)
    assert heldout.retro_audit(ws)["iterations"][0]["verdict"] == "contaminated"


def test_t9g_same_calls_with_heldout_data_still_void(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _put_eval_data(ws, "t")
    _role_calls(monkeypatch, "maintainer", _probe_calls(ws))
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert h["r_val"] is None and "read eval data" in h["proposal"], h
    assert json.load(open(os.path.join(ws, "runs", "maintain-01", "run.json")))["heldout_present"] is True


def test_t9g_role_deleting_eval_still_void(tmp_path, answers, monkeypatch):
    import shutil
    ws = _ws(tmp_path, answers)
    _put_eval_data(ws, "t")
    _role_calls(monkeypatch, "maintainer", [("terminal", {"command": "ls eval"})])
    orig = harness.maintain_step

    def step_then_delete(*a, **kw):
        out = orig(*a, **kw)
        shutil.rmtree(os.path.join(ws, "eval"))  # the role removes the evidence before it ends
        return out
    monkeypatch.setattr(harness, "maintain_step", step_then_delete)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "read eval data" in state["history"][-1]["proposal"]


def test_t9g_f8_unaffected_without_heldout_data(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, "maintainer", [("read_file", {"path": outside + "/test.json"})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "out-of-workspace access" in state["history"][-1]["proposal"]
