"""T8 / QA-F8 + QA-F9: maintainer/proposer confinement and the val-hash oracle.

A typed path outside <ws> (not a system path) or a val-oracle pattern → the iteration is
CONTAMINATED: proposal invalid, no gate run, and no held-out text left anywhere under the ws
(incl. dot-dirs, role homes and every git object). Role homes are fresh per run, inside the ws,
HOME == HERMES_HOME, and gone afterwards. Retro-audit is read-only."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from conftest import add_skill, fake_log, fw_ws, mini_tasks
from hermes_wikiskill import evaluate, harness, heldout, tasks as tasks_mod

CANARY = "CANARY-T8-OUTSIDE"


@pytest.fixture
def outside():
    """A dir that is NEVER allowlisted (/tmp/hws-*), holding held-out-looking text."""
    d = tempfile.mkdtemp(prefix="hws-t8-", dir="/tmp")
    with open(os.path.join(d, "test.json"), "w") as f:
        f.write('[{"id": "te-x", "prompt": "%s held-out prompt"}]' % CANARY)
    yield os.path.realpath(d)
    shutil.rmtree(d, ignore_errors=True)


def _ws_texts(ws):
    """Every regular file under ws (dot-dirs included) + every git object, decompressed."""
    out = []
    for dirpath, dirnames, files in os.walk(ws):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                out.append((os.path.relpath(p, ws), open(p, "rb").read()))
        if os.path.basename(dirpath) == ".git":
            objs = subprocess.run(["git", "--git-dir", dirpath, "cat-file", "--batch-all-objects", "--batch"],
                                  capture_output=True).stdout
            out.append((os.path.relpath(dirpath, ws) + ":objects", objs))
    return out


def _canary_hits(ws, needle=CANARY):
    return [p for p, b in _ws_texts(ws) if needle.encode() in b]


def _evolve(tmp_path, answers, monkeypatch, calls, *, role="maintainer"):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    calls = calls(ws) if callable(calls) else calls
    monkeypatch.setenv("FAKE_HERMES_ROLE_CALLS", json.dumps([[role, t, a] for t, a in calls]))
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    return ws, state


def _digest(ws):
    return sorted(heldout.sealed_digests(ws))[0]


POSITIVES = {
    "abs-gen-py": lambda ws, out: [("read_file", {"path": "/home/x/ops/hermes-wikiskill-src/benches/house-report/gen.py"})],
    # T8b: `~`/$HOME are the role home <ws>/.hws-roles/r-<id> — escaping it means climbing out of the ws
    "tilde-escape": lambda ws, out: [("terminal", {"command": "cat ~/../../../ops/hermes-wikiskill-src/benches/house-report/test.json"})],
    "home-var-escape": lambda ws, out: [("terminal", {"command": "ls $HOME/../../../ops ${HOME}/../../../sibling"})],
    "dotdot-sibling": lambda ws, out: [("read_file", {"path": "../sibling/tasks.json"})],
    "find-root": lambda ws, out: [("terminal", {"command": "find / -name tasks.json 2>/dev/null"})],
    "script-content": lambda ws, out: [("write_file", {"path": ws + "/scratch.py",
                                                       "content": "print(open('%s/test.json').read())\n" % out})],
    "read-outside-file": lambda ws, out: [("read_file", {"path": out + "/test.json"})],
    "symlink-out": lambda ws, out: [("read_file", {"path": ws + "/wiki/escape/test.json"})],
    "oracle-pair": lambda ws, out: [("write_file", {"path": ws + "/brute.py", "content":
                                     "import hashlib\nT=[t['grader']['expected_sha256'] for t in tasks]\n"})],
    "oracle-digest": lambda ws, out: [("terminal", {"command": "echo " + _digest(ws)})],
}


@pytest.mark.parametrize("case", sorted(POSITIVES))
@pytest.mark.parametrize("role", ["maintainer", "proposer"])
def test_t8_positive_invalidates_iteration(tmp_path, answers, monkeypatch, outside, case, role):
    def calls(ws):
        if case == "symlink-out":
            os.symlink(outside, os.path.join(ws, "wiki", "escape"))
        return POSITIVES[case](ws, outside)

    ws, state = _evolve(tmp_path, answers, monkeypatch, calls, role=role)
    h = state["history"][-1]
    kind = "val-oracle" if case.startswith("oracle") else "out-of-workspace access"
    assert h["r_val"] is None and not h["accepted"], h
    assert ("contaminated: " + kind) in h["proposal"] and role in h["proposal"], h["proposal"]
    assert not os.path.exists(os.path.join(ws, "runs", "iter-01", "val"))          # no gate run
    assert _canary_hits(ws) == []                                                   # incl. git objects
    assert state["r_best"] == state["baseline"]
    if role == "maintainer":
        assert not os.path.exists(os.path.join(ws, "wiki", "patterns", "fake-pattern.md"))


NEGATIVE_CALLS = [
    ("write_file", {"path": "notes.py", "content": "q = a // b\nr = a / b\np = 'a/b'\nsed = 's/x/y/'\n"
                                                    "url = 'https://example.com/x/y'\n"
                                                    "for t in tasks:\n    if 'expected_sha256' in t['grader']: continue\n"}),
    ("terminal", {"command": "python3 notes.py 2>/dev/null; /usr/bin/python3 -V; cat /etc/hostname"}),
    ("terminal", {"command": "python3 /tmp/scratch.py > /dev/null"}),
    ("terminal", {"command": "ls && cat raw/traces/iter-01/train/tr-0.meta.json && cat tasks.json"}),
    ("read_file", {"path": "wiki/index.md"}),
]


def test_t8_negatives_are_accepted_normally(tmp_path, answers, monkeypatch):
    ws, state = _evolve(tmp_path, answers, monkeypatch, lambda ws: NEGATIVE_CALLS + [
        ("read_file", {"path": ws + "/raw/traces/iter-01/train/tr-0.meta.json"})])
    h = state["history"][-1]
    assert h["r_val"] is not None and "contaminated" not in json.dumps(state["history"]), h
    sess = json.load(open(os.path.join(ws, "runs", "maintain-01", "session.jsonl")))
    typed = json.dumps([c["function"] for m in sess["messages"] for c in m.get("tool_calls") or []])
    assert "/usr/bin/python3" in typed and "a // b" in typed and "raw/traces" in typed   # really exercised


def test_t8_detectors_unit(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    s = {"messages": [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": c})}}
        for c in NEGATIVE_CALLS[1][1]["command"].split("; ")]}]}
    assert heldout.detect_outside(s, ws) == []
    assert heldout.allowed_outside("/tmp/scratch.py", ws) and not heldout.allowed_outside("/tmp/hws-x/y", ws)
    assert not heldout.allowed_outside(str(tmp_path / "other-ws" / "tasks.json"), ws)      # a sibling ws
    assert not heldout.allowed_outside(os.environ["HWS_ROLLOUT_ROOT"] + "/r-1/home/.env", ws)
    assert heldout.path_candidates("x = y /100", ws=ws, home="/h") == []
    assert heldout.path_candidates("find / -type f", ws=ws, home="/h", command=True) == ["/"]


# ---------------------------------------------------------------- B: role homes
def test_t8_role_home_is_fresh_inside_ws_and_removed(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.maintain_step(ws, 1, [])
    harness.propose_step(ws, 1, [])
    roles = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]
             and os.path.realpath(r["env"]["HERMES_HOME"]).startswith(os.path.realpath(ws))]
    assert len(roles) == 2
    homes = {r["env"]["HERMES_HOME"] for r in roles}
    assert len(homes) == 2                                                    # fresh per run
    for r in roles:
        home = r["env"]["HERMES_HOME"]
        assert r["home_env"] == home and os.path.dirname(home) == os.path.join(ws, ".hws-roles")
        assert not os.path.exists(home)                                       # removed after export
    assert not os.path.exists(os.path.join(ws, ".hws-roles"))
    assert os.path.isfile(os.path.join(ws, "runs", "maintain-01", "session.jsonl"))  # export happened first


# ---------------------------------------------------------------- C: no abs held-out path
def test_t8_eval_summary_tasks_file_is_basename(tmp_path, answers, outside):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    tf = os.path.join(outside, "held-out.json")
    with open(tf, "w") as f:
        json.dump(tasks_mod.seal_split([dict(ts[2], id="te-1", split="test")]), f)
    s = evaluate.evaluate(ws, tf, skills="empty", repeat=1, tag="t8")
    assert s["tasks_file"] == "held-out.json"
    assert [p for p, b in _ws_texts(ws) if outside.encode() in b] == []


# ---------------------------------------------------------------- F: retro-audit
def _tree_digest(ws):
    h = hashlib.sha256()
    for dirpath, dirnames, files in sorted(os.walk(ws)):
        dirnames.sort()
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            h.update(p.encode() + str(st.st_mtime_ns).encode())
            if not os.path.islink(p):
                h.update(open(p, "rb").read())
    return h.hexdigest()


def test_t8_retro_audit_is_read_only_and_finds_both_verdicts(tmp_path, answers, monkeypatch, outside):
    ws, _ = _evolve(tmp_path, answers, monkeypatch, [])                         # iter 1: clean
    monkeypatch.setenv("FAKE_HERMES_ROLE_CALLS", "[]")
    before = _tree_digest(ws)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, "-m", "hermes_wikiskill.heldout", ws, "--json"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    assert [i["verdict"] for i in r["iterations"]] == ["clean"]
    assert set(r["iterations"][0]["roles"]) == {"maintainer", "proposer"}
    assert _tree_digest(ws) == before                                          # wrote nothing
    # a session that reached outside (not redacted, e.g. recorded by a pre-T8 install) → contaminated
    sf = os.path.join(ws, "runs", "maintain-01", "session.jsonl")
    sess = json.load(open(sf))
    sess["messages"].append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "z", "type": "function", "function": {"name": "read_file",
                                                     "arguments": json.dumps({"path": outside + "/test.json"})}}]})
    open(sf, "w").write(json.dumps(sess) + "\n")
    r2 = heldout.retro_audit(ws)
    assert r2["iterations"][0]["verdict"] == "contaminated"
    assert "out-of-workspace access" in r2["iterations"][0]["note"]
    assert heldout.main([ws]) == 1


def test_t8_real_session_shapes_are_not_flagged(tmp_path):
    """False positives found by the host retro-audit of eff-house-report iter 1 (a clean run):
    shell `cd` moves the cwd (`cd wiki && ... && cd .. && python3 .hermes-home/...`), and prose
    written to the wiki contains ellipses / bare `..`."""
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    cmds = ["cd %s && ls -R skills/active | head -40; cd wiki && git log --oneline | head; cat ../workspace.json" % ws,
            "cd %s/wiki && git status --short && cd .. && python3 .hermes-home/cache/scratch/verify_rule.py | grep x" % ws]
    prose = "# Pattern\nIteration 1 analyzed 8 traces ... runs hit 5-14 blocked results .. then retried.\n"
    calls = [{"id": str(i), "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": c})}}
             for i, c in enumerate(cmds)]
    calls.append({"id": "w", "type": "function", "function": {"name": "write_file", "arguments": json.dumps(
        {"path": ws + "/wiki/patterns/p.md", "content": prose})}})
    s = {"messages": [{"role": "assistant", "content": "", "tool_calls": calls}]}
    assert heldout.detect_outside(s, ws) == []
    bad = {"messages": [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "b", "type": "function", "function": {"name": "terminal", "arguments": json.dumps(
            {"command": "cd %s/wiki && cd ../.. && cat other-ws/tasks.json" % ws})}}]}]}
    assert heldout.detect_outside(bad, ws)                    # the tracked cwd really left the ws


# ---------------------------------------------------------------- T8b: no false positives on the role's own files
def _calls_session(calls):
    return {"messages": [{"role": "assistant", "content": "", "tool_calls": [
        {"id": str(i), "type": "function", "function": {"name": t, "arguments": json.dumps(a)}}
        for i, (t, a) in enumerate(calls)]}]}


OWN_FILES = [("read_file", {"path": "~/skills/wikiskill-maintainer/SKILL.md"}),
             ("terminal", {"command": "cd ~ && ls && cat $HOME/skills/wikiskill-proposer/SKILL.md"}),
             ("read_file", {"path": ".hws-roles/r-me/skills/wikiskill-proposer/SKILL.md"})]


def test_t8b_role_home_paths_are_inside_the_ws(tmp_path):
    ws = str(tmp_path / "ws")
    home = os.path.join(ws, ".hws-roles", "r-me")
    os.makedirs(home)
    assert heldout.detect_outside(_calls_session(OWN_FILES), ws, home=home) == []
    for esc in ("~/../../../other-ws/tasks.json", "/home/u/ops/hermes-wikiskill-src/benches/x/gen.py"):
        assert heldout.detect_outside(_calls_session([("read_file", {"path": esc})]), ws, home=home), esc
    assert heldout.detect_outside(_calls_session([("terminal", {"command": "cd ~/../../.. && cat other-ws/t.json"})]),
                                  ws, home=home)


def test_t8b_live_role_reading_its_own_skills_still_gates(tmp_path, answers, monkeypatch):
    def calls(ws):
        rel = [c for c in OWN_FILES if not c[1].get("path", "").startswith(".hws-roles")]
        return rel + [("read_file", {"path": ".hws-roles/%s/skills/wikiskill-maintainer/SKILL.md"
                                               % "r-any"})]

    ws, state = _evolve(tmp_path, answers, monkeypatch, calls)
    h = state["history"][-1]
    assert h["r_val"] is not None and "contaminated" not in json.dumps(state["history"]), h
    rec = json.load(open(os.path.join(ws, "runs", "maintain-01", "run.json")))
    assert os.path.dirname(rec["role_home"]) == os.path.join(ws, ".hws-roles")
    roles = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"] and "--help" not in r["argv"]
             and ".hws-roles" in r["env"]["HERMES_HOME"]]
    assert roles and all(r["skill_links"] == [] for r in roles)          # copies, never symlinks
    # retro-audit: the recorded role home → clean; a legacy run.json without it → operator HOME → flagged
    assert heldout.retro_audit(ws)["iterations"][0]["verdict"] == "clean"
    rp = os.path.join(ws, "runs", "maintain-01", "run.json")
    rec.pop("role_home")
    open(rp, "w").write(json.dumps(rec))
    r = heldout.retro_audit(ws)["iterations"][0]
    assert r["verdict"] == "contaminated" and r["roles"]["maintainer"]["home"] == "operator"
