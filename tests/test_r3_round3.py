"""T11 / ORCH Round 3 (INTERFACE §8): R3-01 — a bad role run is undone for the WHOLE workspace except an
allowlist of harness records; retro_audit reports laundering; m1 nested repos, m2 `hws reset` after a hard
kill, m3 `transfer` from the gated commit. Pinned from the orchestrator probes
(.cmux-team/orch-probes/test_zz_{causal,hardkill,r204_r205}.py) with the repo fake."""
import json
import os
import subprocess

import pytest

from conftest import add_skill, fw_ws, hws, mini_tasks
from hermes_wikiskill import evaluate, gating, gitutil, harness, heldout, transfer
from hermes_wikiskill.errors import RolloutError
from test_t8_confinement import CANARY, _canary_hits, outside  # noqa: F401 (fixture)


def _ws(tmp_path, answers, name="ws"):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts, name=name)
    add_skill(ws)
    return ws


def _role_calls(monkeypatch, calls):
    monkeypatch.setenv("FAKE_HERMES_ROLE_CALLS", json.dumps([list(c) for c in calls]))


def _read_outside(outside, role="maintainer"):
    return (role, "read_file", {"path": outside + "/test.json"})


def _write(path, content, role="maintainer"):
    return (role, "write_file", {"path": path, "content": content})


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    import tempfile
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return str(d)


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


# ============================================================== R3-01 void path: every channel restored
CHANNELS = ["runs/maintain-01/notes.md",  # own run dir: only the harness records are kept
            ".hws-roles/notes.md",        # role-home parent: only r-* role homes are kept
            "wiki/.git/NOTES",            # the WHOLE wiki git dir, not only gitutil.META
            "skills/active/.git/NOTES",   # the WHOLE skills/active git dir
            ".hws.lock"]                  # the lock file's bytes


@pytest.mark.parametrize("chan", CHANNELS)
def test_t11_void_run_leaves_nothing_in_any_channel(tmp_path, answers, monkeypatch, outside, chan):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside), _write(chan, "%s held-out prompt\n" % CANARY)])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "contaminated" in state["history"][-1]["proposal"]
    assert _canary_hits(ws) == []
    names = set(os.listdir(os.path.join(ws, "runs", "maintain-01")))  # the allowlisted records stay
    assert {"query.txt", "session.jsonl", "run.json", "confinement.json"} <= names


# ============================================================== R3-01 raise path (c05, c06)
def test_t11_raise_path_state_json_is_the_pre_role_copy(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=0, verbose=False, no_early_stop=True)
    sp = os.path.join(ws, "runs", "state.json")
    st0 = json.load(open(sp))
    _role_calls(monkeypatch, [_read_outside(outside),
                              _write("runs/state.json", json.dumps({**st0, "notes": "%s held-out" % CANARY}))])
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert CANARY not in open(sp).read()
    assert "role_running" not in json.load(open(sp))


def test_t11_clean_run_state_json_edits_are_not_kept(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=0, verbose=False, no_early_stop=True)
    sp = os.path.join(ws, "runs", "state.json")
    before = open(sp, "rb").read()

    def step():  # a CLEAN role run (no void) that edits the harness state
        st = json.load(open(sp))
        json.dump({**st, "notes": "role edit"}, open(sp, "w"))
        return {}
    harness.run_role(ws, "maintainer", 1, step, wiki_base=None)
    assert open(sp, "rb").read() == before


def test_t11_raise_path_role_file_in_own_run_dir_never_archived(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside), _write("runs/maintain-01/notes.md", "%s held-out\n" % CANARY)])
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    monkeypatch.delenv("FAKE_HERMES_ROLE_EXIT")
    monkeypatch.delenv("FAKE_HERMES_ROLE_CALLS")
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    rdir = os.path.join(ws, "runs", "maintain-01")
    found = [os.path.relpath(os.path.join(d, f), rdir) for d, _, fs in os.walk(rdir) for f in fs if f == "notes.md"]
    assert found == [] and _canary_hits(ws) == []


def test_t11_archive_attempt_moves_only_harness_records(tmp_path):
    rdir = tmp_path / "runs" / "maintain-01"
    rdir.mkdir(parents=True)
    for n in ("query.txt", "run.json", "notes.md"):
        (rdir / n).write_text(n)
    (rdir / "sub").mkdir()
    (rdir / "sub" / "x.md").write_text("x")
    dst = harness.archive_attempt(str(rdir))
    assert sorted(os.listdir(dst)) == ["query.txt", "run.json"]
    assert sorted(os.listdir(rdir)) == ["attempt-1"]


# ============================================================== R3-01 hard kill: state reloaded after the restore
def test_t11_hard_kill_resume_drops_killed_roles_state_edits(tmp_path, answers, monkeypatch, private_tmp):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)
    sp = os.path.join(ws, "runs", "state.json")
    st = json.load(open(sp))
    st["notes"] = "%s held-out" % CANARY  # the killed role's edit (it kept role_running to be resumed)
    json.dump(st, open(sp, "w"))
    state = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "notes" not in state and CANARY not in open(sp).read()
    assert [n for n in os.listdir(private_tmp) if n.startswith("hws-rolesnap-")] == []


# ============================================================== R3-01 retro_audit reports laundering (c07)
def test_t11_retro_audit_reports_laundering_attempt(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside), _write("runs/maintain-01/notes.md", "%s held-out\n" % CANARY)])
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    # besides the laundering read: records of the voided run + harness files -> never counted (no false verdicts)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": p}) for p in (
        "runs/maintain-01/notes.md", "runs/maintain-01/run.json", "runs/maintain-01/confinement.json",
        "runs/state.json", "runs/proposals/iter-01.json", "wiki/log.md")])
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    ra = {i["iter"]: i for i in heldout.retro_audit(ws)["iterations"]}
    assert ra[1]["verdict"] == "contaminated"
    assert ra[2]["verdict"] == "laundering-attempt", ra[2]
    assert ra[2]["roles"]["maintainer"]["quarantined_reads"] == ["runs/maintain-01/notes.md"]
    assert heldout.main([ws]) == 1


# ============================================================== m1 nested repositories (n01, n02)
def test_t11_nested_repo_in_tracked_wiki_dir_is_dirty_and_purged(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    pat = os.path.join(ws, "wiki", "patterns")
    _git(pat, "init", "-q")  # a repo INSIDE a tracked dir: git status does not report it at all
    open(os.path.join(pat, ".git", "NOTES"), "w").write("%s\n" % CANARY)
    sub = os.path.join(ws, "wiki", "sub")
    os.makedirs(sub)
    _git(sub, "init", "-q")
    open(os.path.join(sub, "leak.md"), "w").write("%s\n" % CANARY)
    assert not gitutil.is_clean(os.path.join(ws, "wiki"), ignored=True)
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []
    assert gitutil.nested_git(os.path.join(ws, "wiki")) == []


def test_t11_fail_closed_resume_purges_nested_wiki_repo(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    st = gating.load_state(ws)
    st["role_running"] = {"role": "maintainer", "active_head": gating.active_head(ws), "snapshot": None,
                          "k": 2, "started_ns": 1}
    gating.save_state(ws, st)
    sub = os.path.join(ws, "wiki", "sub")
    os.makedirs(sub)
    _git(sub, "init", "-q")
    _git(sub, "commit", "-q", "--allow-empty", "-m", "x")
    open(os.path.join(sub, "leak.md"), "w").write("%s\n" % CANARY)
    with pytest.raises(Exception):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert not os.path.exists(sub)


def test_t11_nested_repo_in_skills_active_is_dirty_and_reset(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    d = os.path.join(ws, "skills", "active", "house-report")
    _git(d, "init", "-q")
    open(os.path.join(d, ".git", "NOTES"), "w").write("x\n")
    assert gating.active_dirty(ws) == ["house-report/.git"]
    gating.reset_active(ws)
    assert gating.active_dirty(ws) == [] and os.path.isfile(os.path.join(d, "SKILL.md"))


# ============================================================== m2 `hws reset` after a hard kill (hk03)
def test_t11_reset_after_hard_kill_quarantines_first(tmp_path, answers, private_tmp):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)
    open(os.path.join(ws, "notes.md"), "w").write("%s held-out\n" % CANARY)
    r = hws("reset", ws, env={"TMPDIR": private_tmp})
    assert r.returncode == 0, r.stderr
    assert not os.path.exists(os.path.join(ws, "notes.md"))
    assert not os.path.exists(snap)
    assert _canary_hits(ws) == []


# ============================================================== m3 transfer copies the gated commit
def test_t11_transfer_after_kill_copies_only_gated_skills(tmp_path, answers, private_tmp):
    a = _ws(tmp_path, answers)
    b = fw_ws(tmp_path, mini_tasks(), name="wsb")
    st = gating.load_state(a)
    st["role_running"] = {"role": "proposer", "active_head": gating.active_head(a), "snapshot": None, "k": 2,
                          "started_ns": 1}
    gating.save_state(a, st)
    add_skill(a, "leaked", body="FAKE-SOLVE ungated", commit=False)
    with open(os.path.join(a, "skills", "active", "house-report", "SKILL.md"), "a") as f:
        f.write("UNGATED EDIT\n")
    m = transfer.transfer_skills(a, b)
    assert m["transferred"] == ["house-report"]
    got = open(os.path.join(b, "skills", "active", "house-report", "SKILL.md")).read()
    assert "UNGATED EDIT" not in got and not os.path.exists(os.path.join(b, "skills", "active", "leaked"))
    assert [n for n in os.listdir(private_tmp) if n.startswith("hws-")] == []
    assert evaluate.gated_commit(a) == st["role_running"]["active_head"]


# ============================================================== T11b QA-F30: path-token equality, not substring
def _meta_void_with_root_notes(tmp_path, answers, monkeypatch):
    """Iteration 1: a git-metadata void (no contamination) whose quarantine removes root-level notes.md."""
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_write("wiki/.git/info/NOTE", "x\n"), _write("notes.md", "draft\n")])
    s1 = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "git metadata" in s1["history"][0]["proposal"] and not os.path.exists(os.path.join(ws, "notes.md"))
    return ws


def test_t11b_other_file_with_same_name_is_not_laundering(tmp_path, answers, monkeypatch):
    """qa repro (QA-F30): after root notes.md was quarantined, a clean role reading wiki/patterns/notes.md
    (and naming runs/notes.md in a command) is CLEAN; the heldout CLI exits 0."""
    ws = _meta_void_with_root_notes(tmp_path, answers, monkeypatch)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": "wiki/patterns/notes.md"}),
                              ("maintainer", "terminal", {"command": "cat runs/notes.md wiki/notes.md.bak"})])
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    ra = {i["iter"]: i for i in heldout.retro_audit(ws)["iterations"]}
    assert ra[2]["verdict"] == "clean", ra[2]
    assert "quarantined_reads" not in ra[2]["roles"]["maintainer"]
    assert heldout.main([ws]) == 0


@pytest.mark.parametrize("variant", ["read_file", "dot-slash", "terminal-cat", "absolute"])
def test_t11b_same_path_still_laundering(tmp_path, answers, monkeypatch, variant):
    """True positives stay flagged (iteration 3), next to the QA-F30 look-alike read (iteration 2, clean)."""
    ws = _meta_void_with_root_notes(tmp_path, answers, monkeypatch)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": "wiki/patterns/notes.md"})])
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    call = {"read_file": ("maintainer", "read_file", {"path": "notes.md"}),
            "dot-slash": ("maintainer", "read_file", {"path": "./notes.md"}),
            "terminal-cat": ("maintainer", "terminal", {"command": "cat 'notes.md' | head -5"}),
            "absolute": ("maintainer", "read_file", {"path": os.path.join(ws, "notes.md")})}[variant]
    _role_calls(monkeypatch, [call])
    harness.evolve(ws, iters=3, verbose=False, no_early_stop=True)
    ra = {i["iter"]: i for i in heldout.retro_audit(ws)["iterations"]}
    assert ra[2]["verdict"] == "clean", ra[2]
    assert ra[3]["verdict"] == "laundering-attempt", ra[3]
    assert ra[3]["roles"]["maintainer"]["quarantined_reads"] == ["notes.md"]
    assert heldout.main([ws]) == 1


# ============================================================== T11c QA-F31: records kept by PROVENANCE, not by name
def test_t11c_void_role_written_query_txt_is_the_harness_copy(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside),
                              _write("runs/maintain-01/query.txt", "%s held-out prompt\n" % CANARY)])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "contaminated" in state["history"][-1]["proposal"]
    q = open(os.path.join(ws, "runs", "maintain-01", "query.txt")).read()
    assert CANARY not in q and "Wiki Maintainer" in q  # the prompt the harness wrote
    assert _canary_hits(ws) == []


def test_t11c_raise_role_written_query_txt_never_archived(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside),
                              _write("runs/maintain-01/query.txt", "%s held-out prompt\n" % CANARY)])
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []
    monkeypatch.delenv("FAKE_HERMES_ROLE_EXIT")
    monkeypatch.delenv("FAKE_HERMES_ROLE_CALLS")
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    a1 = open(os.path.join(ws, "runs", "maintain-01", "attempt-1", "query.txt")).read()
    assert CANARY not in a1 and "Wiki Maintainer" in a1
    assert _canary_hits(ws) == []


def _killed_maintainer_with_records(ws, *, with_provenance):
    """Iteration 2's maintainer SIGKILLed mid-run (no finally) after writing CANARY under every record name
    of its own run dir; ``with_provenance``: the backend had already written + recorded query.txt."""
    from hermes_wikiskill.backends import hermes as backend
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    rdir = harness.role_run_dir(ws, "maintainer", 2)
    harness.archive_attempt(rdir)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)
    os.makedirs(rdir, exist_ok=True)
    if with_provenance:
        backend.PROVENANCE_DIR = snap
        try:
            open(os.path.join(rdir, "query.txt"), "w").write("You are the Wiki Maintainer (harness prompt)\n")
            backend._provenance(os.path.join(rdir, "query.txt"), keep_copy=True)
        finally:
            backend.PROVENANCE_DIR = None
    for n in harness.HARNESS_RECORDS:  # the killed role's own writes under the record names
        open(os.path.join(rdir, n), "w").write('{"x": "%s held-out"}\n' % CANARY)
    return rdir, snap


@pytest.mark.parametrize("with_provenance", [False, True], ids=["no-provenance", "query-recorded"])
def test_t11c_hard_kill_records_are_not_archived_as_harness_records(tmp_path, answers, monkeypatch,
                                                                    private_tmp, with_provenance):
    ws = _ws(tmp_path, answers)
    rdir, snap = _killed_maintainer_with_records(ws, with_provenance=with_provenance)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": "runs/maintain-02/attempt-1/" + n})
                              for n in harness.HARNESS_RECORDS])
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []
    a1 = os.path.join(rdir, "attempt-1")
    kept = sorted(os.listdir(a1)) if os.path.isdir(a1) else []
    if with_provenance:  # the harness's own query (restored from its copy) + its fresh quarantine run.json
        assert open(os.path.join(a1, "query.txt")).read() == "You are the Wiki Maintainer (harness prompt)\n"
        assert set(kept) == {"query.txt", "run.json"}
    else:
        assert set(kept) <= {"run.json"}
    if "run.json" in kept:
        assert set(json.load(open(os.path.join(a1, "run.json")))) <= {"quarantine", "started_ns"}
    assert not os.path.exists(snap)


# ============================================================== T11d QA-F32: bad run + lost provenance = fail closed
@pytest.mark.parametrize("path", ["void", "raise"])
def test_t11d_bad_run_without_provenance_keeps_no_role_query(tmp_path, answers, monkeypatch, outside, path):
    from hermes_wikiskill.backends import hermes as backend
    ws = _ws(tmp_path, answers)
    monkeypatch.setattr(backend, "_provenance", lambda *a, **kw: None)  # the provenance write fails
    _role_calls(monkeypatch, [_read_outside(outside),
                              _write("runs/maintain-01/query.txt", "%s held-out prompt\n" % CANARY)])
    if path == "raise":
        monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
        with pytest.raises(RolloutError):
            harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    else:
        state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
        assert "contaminated" in state["history"][-1]["proposal"]
    rdir = os.path.join(ws, "runs", "maintain-01")
    assert CANARY not in open(os.path.join(rdir, "query.txt")).read()
    assert json.load(open(os.path.join(rdir, "run.json")))["provenance_unavailable"] is True
    assert _canary_hits(ws) == []
    assert backend.PROVENANCE_EXPECTED == set()


# ============================================================== T11e QA-F34: lost provenance + interrupt before the backend writes
@pytest.mark.parametrize("where", ["spawn", "export"])
def test_t11e_interrupt_without_provenance_keeps_no_role_session_or_run_json(tmp_path, answers, monkeypatch,
                                                                              outside, where):
    import subprocess as sp
    from hermes_wikiskill.backends import hermes as backend
    ws = _ws(tmp_path, answers)
    monkeypatch.setattr(backend, "_provenance", lambda *a, **kw: None)
    rec = json.dumps({"note": "%s held-out" % CANARY, "heldout_present": CANARY})
    _role_calls(monkeypatch, [_read_outside(outside), _write("runs/maintain-01/session.jsonl", rec + "\n"),
                              _write("runs/maintain-01/run.json", rec)])
    if where == "spawn":
        real = backend._spawn

        def interrupted(*a, **kw):
            rc = real(*a, **kw)
            if backend.PROVENANCE_DIR:
                raise SystemExit(143)
            return rc
        monkeypatch.setattr(backend, "_spawn", interrupted)
    else:
        real = backend.export_session

        def timed_out(*a, **kw):
            if backend.PROVENANCE_DIR:  # the role run (task rollouts run without it)
                raise sp.TimeoutExpired(["hermes", "sessions", "export"], 120)
            return real(*a, **kw)
        monkeypatch.setattr(backend, "export_session", timed_out)
    with pytest.raises((SystemExit, sp.TimeoutExpired)):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    rdir = os.path.join(ws, "runs", "maintain-01")
    assert not os.path.exists(os.path.join(rdir, "session.jsonl"))
    got = json.load(open(os.path.join(rdir, "run.json")))
    assert set(got) <= {"heldout_present", "started_ns", "quarantine", "provenance_unavailable"}
    assert got["provenance_unavailable"] is True and isinstance(got["heldout_present"], bool)
    assert _canary_hits(ws) == []
