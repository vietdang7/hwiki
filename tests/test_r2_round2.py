"""T10 / ORCH Round 2 (INTERFACE §7): role-output quarantine (R2-01), wiki ignored files (R2-03),
retro-audit records (R2-04), eval/gate dirty-state guard (R2-05) — pinned from the orchestrator
probes (.cmux-team/orch-probes/test_zz_verify.py). R2-12 detection limitations stay strict xfail
(no detection-rule change in Round 2)."""
import json
import os
import re
import shutil
import subprocess

import pytest

from conftest import add_skill, fw_ws, hws, mini_tasks
from hermes_wikiskill import evaluate, gating, harness, heldout
from hermes_wikiskill.errors import HwsError, RolloutError
from test_t8_confinement import CANARY, _calls_session, _canary_hits, outside  # noqa: F401 (fixture)

R2_12 = pytest.mark.xfail(strict=True, reason="R2-12 known limitation, documented in README Honest expectations")


def _ws(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    add_skill(ws)
    return ws


def _role_calls(monkeypatch, calls):
    monkeypatch.setenv("FAKE_HERMES_ROLE_CALLS", json.dumps([[r, t, a] for r, t, a in calls]))


def _clear(monkeypatch):
    for k in ("FAKE_HERMES_ROLE_CALLS", "FAKE_HERMES_ROLE_EXIT", "FAKE_HERMES_PROPOSAL"):
        monkeypatch.delenv(k, raising=False)


def _with_results(calls_results):
    calls = [{"id": str(i), "type": "function", "function": {"name": t, "arguments": json.dumps(a)}}
             for i, (t, a, _r) in enumerate(calls_results)]
    msgs = [{"role": "assistant", "content": "", "tool_calls": calls}]
    msgs += [{"role": "tool", "tool_call_id": str(i), "tool_name": t, "content": r}
             for i, (t, _a, r) in enumerate(calls_results)]
    return {"messages": msgs}


def _read_outside(outside, role="maintainer"):
    return (role, "read_file", {"path": outside + "/test.json"})


def _seen_at_maintainer_start(monkeypatch, path):
    """T10b: what the NEXT maintainer can see when it starts (wraps maintain_step)."""
    seen = []
    orig = harness.maintain_step

    def spy(ws, k, *a, **kw):
        seen.append(os.path.lexists(path))
        return orig(ws, k, *a, **kw)
    monkeypatch.setattr(harness, "maintain_step", spy)
    return seen


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    """QA-F25: the harness's hws-rolesnap-/hws-evalhead- dirs go to a PRIVATE tmp (tempfile.tempdir +
    TMPDIR for CLI subprocesses), so leftover checks never see another concurrent pytest's dirs."""
    import tempfile
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return str(d)


def _hws_tmp(d):
    return sorted(n for n in os.listdir(d) if n.startswith(("hws-rolesnap-", "hws-evalhead-")))


def _run_json(ws, tag):
    return json.load(open(os.path.join(ws, "runs", tag, "run.json")))


# ============================================================== R2-03 wiki ignored files
IGNORED = [("maintainer", "write_file", {"path": "wiki/.gitignore", "content": "*\n"}),
           ("maintainer", "write_file", {"path": "wiki/patterns/leak.md",
                                         "content": "# leak\n%s held-out prompt\n" % CANARY})]


def test_v01a_contaminated_maintainer_ignored_leak_is_removed(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside)] + IGNORED)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "contaminated" in state["history"][-1]["proposal"]
    assert _canary_hits(ws) == []
    assert not os.path.exists(os.path.join(ws, "wiki", ".gitignore"))


def test_v01b_raise_path_ignored_leak_removed_and_resume_is_clean(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside)] + IGNORED)
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert not os.path.exists(os.path.join(ws, "wiki", "patterns", "leak.md"))
    _clear(monkeypatch)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []


def test_v01c_hard_kill_resume_removes_ignored_wiki_files(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    with open(os.path.join(ws, "wiki", ".gitignore"), "w") as f:
        f.write("*\n")
    with open(os.path.join(ws, "wiki", "patterns", "leak.md"), "w") as f:
        f.write("# leak\n%s\n" % CANARY)
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert _canary_hits(ws) == []


# ============================================================== R2-01 role output quarantine
def test_v01d_contaminated_maintainer_ws_root_output_quarantined(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside),
                              ("maintainer", "write_file", {"path": "scratch/notes.md",
                                                            "content": "%s held-out prompt\n" % CANARY})])
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = state["history"][-1]
    assert "contaminated" in h["proposal"]
    assert _canary_hits(ws) == []
    assert not os.path.exists(os.path.join(ws, "scratch"))
    q = _run_json(ws, "maintain-01")["quarantine"]
    assert "scratch/notes.md" in q["removed"] and "scratch/" in q["removed"]
    assert any("maintainer quarantined" in n for n in h["role_notes"])


def test_v01e_raising_contaminated_proposer_leaves_no_proposal(tmp_path, answers, monkeypatch, outside):
    """Pin rewritten: the orchestrator probe's resumed fake maintainer typed the canary literally into
    its wiki write; the property is that nothing the voided proposer wrote survives for it to read."""
    ws = _ws(tmp_path, answers)
    bad = {"action": "create", "name": "house-report",
           "skill_md": "---\nname: house-report\ndescription: x\n---\nFAKE-SOLVE %s\n" % CANARY}
    _role_calls(monkeypatch, [_read_outside(outside, "proposer")])
    monkeypatch.setenv("FAKE_HERMES_PROPOSAL", json.dumps(bad))
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "proposer:130")
    with pytest.raises(HwsError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    ppath = harness.proposal_path(ws, 1)
    assert not os.path.exists(ppath)
    assert _canary_hits(ws) == []
    assert os.path.relpath(ppath, ws) in _run_json(ws, "propose-01")["quarantine"]["removed"]
    _clear(monkeypatch)  # the resumed maintainer runs BEFORE propose_step would delete a stale proposal
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": os.path.relpath(ppath, ws)})])
    seen = _seen_at_maintainer_start(monkeypatch, ppath)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert seen == [False]  # T10b: the quarantined proposal does not exist when the next role starts
    assert _canary_hits(ws) == []


def test_v01h_voided_scratch_not_laundered_next_iteration(tmp_path, answers, monkeypatch, outside):
    """Pin rewritten (see v01e): the voided maintainer's root-level notes are gone before iteration 2."""
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside),
                              ("maintainer", "write_file", {"path": "notes.md",
                                                            "content": "%s held-out prompt\n" % CANARY})])
    s1 = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "contaminated" in s1["history"][0]["proposal"]
    assert not os.path.exists(os.path.join(ws, "notes.md")) and _canary_hits(ws) == []
    _clear(monkeypatch)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": "notes.md"})])
    seen = _seen_at_maintainer_start(monkeypatch, os.path.join(ws, "notes.md"))
    s2 = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert seen == [False]  # T10b: the quarantined notes do not exist when the next role starts
    assert s2["history"][-1]["r_val"] is not None
    it2 = [i for i in heldout.retro_audit(ws)["iterations"] if i["iter"] == 2][0]
    # T11 (R3-01, probe c07): reading a path the iteration-1 quarantine removed is REPORTED (not clean)
    assert it2["verdict"] == "laundering-attempt" and it2["roles"]["maintainer"]["quarantined_reads"] == ["notes.md"]
    assert _canary_hits(ws) == []


def _first_trace_file(ws):
    root = os.path.join(ws, "raw", "traces")
    for dp, _dn, fn in sorted(os.walk(root)):
        for f in sorted(fn):
            return os.path.relpath(os.path.join(dp, f), ws)
    raise AssertionError("no raw trace")


def test_voided_maintainer_append_to_raw_trace_restored_byte_identical(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    rel = _first_trace_file(ws)
    before = open(os.path.join(ws, rel), "rb").read()
    _role_calls(monkeypatch, [_read_outside(outside),
                              ("maintainer", "write_file", {"path": rel, "content": before.decode(
                                  "utf-8", "replace") + "\n%s held-out prompt\n" % CANARY})])
    s = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "contaminated" in s["history"][-1]["proposal"]
    assert open(os.path.join(ws, rel), "rb").read() == before
    assert rel in _run_json(ws, "maintain-02")["quarantine"]["restored"]
    assert _canary_hits(ws) == []


def _quarantined(ws, mutate):
    """Snapshot → role-like mutation → quarantine (what run_role does for a bad run)."""
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    try:
        mutate()
        return harness._quarantine(ws, snap)
    finally:
        shutil.rmtree(snap, ignore_errors=True)


def test_quarantine_restores_deleted_and_removes_created(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    tjson = os.path.join(ws, "tasks.json")
    tbytes = open(tjson, "rb").read()

    def mutate():
        os.remove(tjson)
        os.makedirs(os.path.join(ws, "new", "deep"))
        open(os.path.join(ws, "new", "deep", "x.md"), "w").write("x")

    q = _quarantined(ws, mutate)
    assert q["restored"] == ["tasks.json"] and open(tjson, "rb").read() == tbytes
    assert set(q["removed"]) == {"new/deep/x.md", "new/deep/", "new/"} and not os.path.exists(os.path.join(ws, "new"))


def test_quarantine_never_writes_through_a_role_symlink(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    rel = _first_trace_file(ws)
    top = rel.split(os.sep)[0]  # "raw"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def mutate():
        shutil.move(os.path.join(ws, top), str(tmp_path / "moved"))
        os.symlink(str(elsewhere), os.path.join(ws, top))

    q = _quarantined(ws, mutate)
    assert top in q["removed"] and rel in q["restored"]
    assert not os.path.islink(os.path.join(ws, top)) and os.path.isfile(os.path.join(ws, rel))
    assert list(elsewhere.iterdir()) == []


def test_clean_run_keeps_its_outputs(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [("maintainer", "write_file", {"path": "wiki/patterns/kept.md", "content": "# kept\n"}),
                              ("maintainer", "write_file", {"path": "scratch/tmp.md", "content": "scratch\n"})])
    s = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    h = s["history"][-1]
    assert h["r_val"] is not None and "role_notes" not in h
    tracked = subprocess.run(["git", "ls-files"], cwd=os.path.join(ws, "wiki"), capture_output=True, text=True).stdout
    assert "patterns/kept.md" in tracked
    assert os.path.isfile(os.path.join(ws, "scratch", "tmp.md"))
    assert os.path.isfile(harness.proposal_path(ws, 1))
    assert "quarantine" not in _run_json(ws, "maintain-01") and "quarantine" not in _run_json(ws, "propose-01")


def _killed_proposer(tmp_path, answers, *, keep_snapshot):
    """Iteration 1 done; iteration 2's proposer SIGKILLed (no finally) after writing a canary proposal
    and a root-level note."""
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    snap = harness._ws_snapshot(ws, "proposer", 2)
    assert os.path.basename(snap).startswith("hws-rolesnap-")
    harness._mark_role(ws, "proposer", gating.active_head(ws), snap, 2)
    os.makedirs(os.path.dirname(harness.proposal_path(ws, 2)), exist_ok=True)
    with open(harness.proposal_path(ws, 2), "w") as f:
        json.dump({"action": "create", "name": "house-report", "skill_md": "FAKE-SOLVE %s" % CANARY}, f)
    with open(os.path.join(ws, "leak.md"), "w") as f:
        f.write("%s\n" % CANARY)
    with open(os.path.join(ws, "wiki", "index.md"), "a") as f:
        f.write("\n%s\n" % CANARY)
    if not keep_snapshot:
        shutil.rmtree(snap)
    return ws, snap


def test_v01e_hard_killed_proposer_quarantined_before_resumed_maintainer(tmp_path, answers, monkeypatch):
    ws, snap = _killed_proposer(tmp_path, answers, keep_snapshot=True)
    rel = os.path.relpath(harness.proposal_path(ws, 2), ws)
    _role_calls(monkeypatch, [("maintainer", "read_file", {"path": rel})])  # the resumed maintainer looks
    monkeypatch.setenv("FAKE_HERMES_PROPOSAL", "none")
    state = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "role_running" not in state and not os.path.exists(snap)
    assert not os.path.exists(os.path.join(ws, "leak.md")) and _canary_hits(ws) == []
    sess = open(os.path.join(ws, "runs", "maintain-02", "session.jsonl")).read()
    assert CANARY not in sess  # the maintainer's read found nothing: deleted before it ran


def test_v01e_hard_killed_proposer_snapshot_missing_fails_closed(tmp_path, answers):
    ws, _snap = _killed_proposer(tmp_path, answers, keep_snapshot=False)
    with pytest.raises(HwsError, match="cannot be verified: leak.md"):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert not os.path.exists(harness.proposal_path(ws, 2))           # stale proposal deleted
    assert CANARY not in open(os.path.join(ws, "wiki", "index.md")).read()  # wiki back to wiki_audited
    assert "role_running" not in gating.load_state(ws)
    os.remove(os.path.join(ws, "leak.md"))                            # the operator inspected
    state = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert state["history"][-1]["iter"] == 2 and _canary_hits(ws) == []


def test_role_snapshot_dir_is_hws_prefixed_and_removed(tmp_path, answers, monkeypatch, private_tmp):
    seen = []
    orig = harness._ws_snapshot

    def spy(*a, **kw):
        d = orig(*a, **kw)
        seen.append(d)
        return d
    monkeypatch.setattr(harness, "_ws_snapshot", spy)
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert len(seen) == 2
    for d in seen:
        assert os.path.dirname(d) == private_tmp and os.path.basename(d).startswith("hws-rolesnap-")
        assert not os.path.exists(d)
    assert _hws_tmp(private_tmp) == []


def test_manual_maintain_verb_quarantines(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside),
                              ("maintainer", "write_file", {"path": "notes.md", "content": "%s\n" % CANARY})])
    r = hws("maintain", ws, "--iter", "1", "--json")
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert not os.path.exists(os.path.join(ws, "notes.md")) and _canary_hits(ws) == []
    assert "quarantined" in r.stdout


# ============================================================== R2-04 retro-audit records
def test_v01f_retro_audit_reads_recorded_state(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside)])
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    it = heldout.retro_audit(ws)["iterations"][0]
    assert it["recorded"] is not None


def test_v01g_raise_path_contamination_reported_and_noted(tmp_path, answers, monkeypatch, outside):
    """Pin rewritten to the EXPECTED behaviour (the probe only printed the buggy state)."""
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside)])
    monkeypatch.setenv("FAKE_HERMES_ROLE_EXIT", "maintainer:130")
    with pytest.raises(RolloutError):
        harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    _clear(monkeypatch)
    state = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert os.path.exists(os.path.join(ws, "runs", "maintain-01", "attempt-1", "confinement.json"))
    it = heldout.retro_audit(ws)["iterations"][0]
    assert it["verdict"] == "contaminated", it
    notes = state["history"][-1].get("role_notes") or []
    assert any("maintainer attempt-1 voided" in n for n in notes), notes


# ============================================================== R2-02 guard (active) — kept probes
def test_v02a_gitignore_star_in_active(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [("proposer", "write_file", {"path": "skills/active/.gitignore", "content": "*\n"}),
                              ("proposer", "write_file", {"path": "skills/active/leaked/SKILL.md",
                                                          "content": "---\nname: leaked\ndescription: x\n---\nx\n"})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert not os.path.exists(os.path.join(ws, "skills/active/leaked"))
    assert h["r_val"] is None


def test_v02b_info_exclude_in_active(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [("proposer", "write_file", {"path": "skills/active/.git/info/exclude", "content": "leaked/\n"}),
                              ("proposer", "write_file", {"path": "skills/active/leaked/SKILL.md",
                                                          "content": "---\nname: leaked\ndescription: x\n---\nx\n"})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert not os.path.exists(os.path.join(ws, "skills/active/leaked"))
    assert h["r_val"] is None


# ============================================================== R2-05 dirty-state guard
def _test_file(tmp_path):
    tf = tmp_path / "test.json"
    tf.write_text(json.dumps([{"id": "te-0", "split": "test", "title": "t", "prompt": "p",
                               "sandbox": {"a.txt": "x"}, "grader": {"type": "exact", "file": "report.txt",
                                                                      "expected": "== OK ==\n"}}]))
    return str(tf)


def test_v02c_hard_killed_proposer_then_eval_uses_gated_head(tmp_path, answers, monkeypatch, capsys):
    """Orchestrator probe v02c (original assertion): eval right after a SIGKILLed proposer that wrote
    skills/active/leaked (legacy marker, no snapshot) measures HEAD, never the working tree."""
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    st = gating.load_state(ws)
    st["role_running"] = {"role": "proposer", "active_head": gating.active_head(ws)}
    gating.save_state(ws, st)
    add_skill(ws, "leaked", body="ungated", commit=False)
    s = evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="after-kill")
    assert "leaked" not in s["skill_names"] and s["skill_names"] == ["house-report"]
    assert s["active_commit"] == gating.active_head(ws)
    assert not os.path.exists(os.path.join(ws, "eval", "after-kill", "skills", "leaked"))
    assert "interrupted" in s["ungated_tree_ignored"] and "warning" in capsys.readouterr().err
    assert os.path.isdir(os.path.join(ws, "skills", "active", "leaked"))  # eval never touches skills/active


def test_eval_takes_head_skills_gate_refuses_ungated_tree(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    with open(os.path.join(ws, "skills", "active", "house-report", "SKILL.md"), "a") as f:
        f.write("UNGATED EDIT\n")  # tracked change
    add_skill(ws, "leaked", body="ungated", commit=False)  # untracked, no role_running
    s = evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="x")
    assert s["skill_names"] == ["house-report"] and "ungated changes" in s["ungated_tree_ignored"]
    assert "UNGATED EDIT" not in open(os.path.join(ws, "eval", "x", "skills", "house-report", "SKILL.md")).read()
    r = hws("gate", ws, "--json")
    assert r.returncode != 0 and "refused" in (r.stdout + r.stderr), (r.returncode, r.stdout, r.stderr)
    assert "ungated_tree_ignored" not in evaluate.evaluate(ws, _test_file(tmp_path), skills="empty", tag="e")


def test_clean_eval_has_no_warning_and_same_skills(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    s = evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="clean")
    assert s["skill_names"] == ["house-report"] and "ungated_tree_ignored" not in s


# ============================================================== T10b / QA Round 6 H1-H4
def _snap_ws(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    return ws, _first_trace_file(ws)


def test_h2_invalid_manifest_fails_closed_without_deletion(tmp_path, answers):
    ws, rel = _snap_ws(tmp_path, answers)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    try:
        for bad in ("", "{not json", json.dumps({"files": {}, "dirs": {}}),
                    json.dumps({**json.load(open(os.path.join(snap, "manifest.json"))), "files": {}})):
            with open(os.path.join(snap, "manifest.json"), "w") as f:
                f.write(bad)
            before = sorted(os.listdir(ws))
            with pytest.raises(HwsError, match="nothing was deleted"):
                harness._quarantine(ws, snap)
            assert sorted(os.listdir(ws)) == before and os.path.isfile(os.path.join(ws, rel))
    finally:
        shutil.rmtree(snap, ignore_errors=True)


def test_h2_resume_with_invalid_manifest_fails_closed(tmp_path, answers):
    ws, rel = _snap_ws(tmp_path, answers)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)  # SIGKILL
    open(os.path.join(snap, "manifest.json"), "w").write("{}")
    with pytest.raises(HwsError, match="unusable pre-role snapshot"):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert os.path.isfile(os.path.join(ws, rel)) and os.path.isfile(os.path.join(ws, "tasks.json"))
    assert "role_running" not in gating.load_state(ws) and not os.path.exists(snap)


def test_h3_missing_content_copy_fails_closed_naming_file(tmp_path, answers):
    ws, rel = _snap_ws(tmp_path, answers)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    try:
        with open(os.path.join(ws, rel), "a") as f:
            f.write("changed by the role\n")
        open(os.path.join(ws, "created.md"), "w").write("x")
        os.remove(os.path.join(snap, "content", rel))
        with pytest.raises(HwsError, match=re.escape(rel)):
            harness._quarantine(ws, snap)
        assert os.path.isfile(os.path.join(ws, rel)) and os.path.isfile(os.path.join(ws, "created.md"))
        with open(os.path.join(snap, "content", rel), "w") as f:
            f.write("corrupt copy")  # a copy whose sha does not match is not used either
        with pytest.raises(HwsError, match=re.escape(rel)):
            harness._quarantine(ws, snap)
        assert "changed by the role" in open(os.path.join(ws, rel)).read()
    finally:
        shutil.rmtree(snap, ignore_errors=True)


def test_h1_legacy_marker_without_k_discards_next_iter_proposal(tmp_path, answers):
    ws, _rel = _snap_ws(tmp_path, answers)
    st = gating.load_state(ws)
    assert st["next_iter"] == 2
    st["role_running"] = {"role": "proposer", "active_head": gating.active_head(ws)}  # legacy: no k/snapshot
    gating.save_state(ws, st)
    pp = harness.proposal_path(ws, 2)
    with open(pp, "w") as f:
        json.dump({"action": "create", "name": "x", "skill_md": CANARY}, f)
    with pytest.raises(HwsError, match="proposal were discarded"):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert not os.path.exists(pp)


def test_h4_chmod_only_change_restored(tmp_path, answers):
    ws, rel = _snap_ws(tmp_path, answers)
    p = os.path.join(ws, rel)
    mode = os.stat(p).st_mode & 0o7777
    d = os.path.dirname(p)
    dmode = os.stat(d).st_mode & 0o7777

    def mutate():
        os.chmod(p, 0o000)
        os.chmod(d, 0o000)

    try:
        q = _quarantined(ws, mutate)
    finally:
        os.chmod(d, dmode) if not os.access(d, os.X_OK) else None
    assert rel in q["restored"]
    assert os.stat(p).st_mode & 0o7777 == mode and os.stat(d).st_mode & 0o7777 == dmode
    assert _canary_hits(ws) == []


# ============================================================== R1-03 probes (kept) + R2-12 limitations
BENCH = "/home/ubuntu/ops/hermes-wikiskill/venv/lib/python3.12/site-packages/hermes_wikiskill/data/benches/house-report"
SRC_BENCH = "/home/ubuntu/ops/hermes-wikiskill-src/benches/house-report"

TRUE_POS = {
    "orig-search-root": ("search_files", {"pattern": "held-out", "path": "/"}),
    "orig-search-parent": ("search_files", {"pattern": "held-out", "path": ".."}),
    "orig-read-parent": ("read_file", {"path": ".."}),
    "orig-ls-pwd-parent": ("terminal", {"command": "ls $PWD/.."}),
    "orig-find-dirname-pwd": ("terminal", {"command": 'find "$(dirname "$PWD")" -name test.json'}),
    "read-outside-testjson": ("read_file", {"path": "OUT/test.json"}),
    "cat-outside-testjson": ("terminal", {"command": "cat OUT/test.json"}),
    "read-bench-test": ("read_file", {"path": BENCH + "/test.json"}),
    "read-bench-readme": ("read_file", {"path": BENCH + "/README.md"}),
    "gen-py": ("terminal", {"command": "python3 %s/gen.py --print" % SRC_BENCH}),
    "ls-bench": ("terminal", {"command": "ls " + SRC_BENCH}),
    "search-bench": ("search_files", {"pattern": "expected", "path": SRC_BENCH}),
    "search-dotdot-slash": ("search_files", {"pattern": "x", "path": "../"}),
    "search-dot-dotdot": ("search_files", {"pattern": "x", "path": "./.."}),
    "search-wiki-up-up": ("search_files", {"pattern": "x", "path": "wiki/../.."}),
    "search-slashdot": ("search_files", {"pattern": "x", "path": "/."}),
    "find-root": ("terminal", {"command": "find / -name test.json 2>/dev/null"}),
    "quoted-abs": ("terminal", {"command": 'cat "%s/test.json"' % SRC_BENCH}),
    "cd-then-cat": ("terminal", {"command": "cd %s && cat test.json" % SRC_BENCH}),
    "pwd-subst": ("terminal", {"command": "ls $(dirname $(pwd))"}),
    "pwd-brace-strip": ("terminal", {"command": "ls ${PWD%/*}"}),
    "pushd": ("terminal", {"command": "pushd .. && ls"}),
    "python-rel-slash": ("terminal", {"command": "python3 -c \"print(open('../x/test.json').read())\""}),
    "read-tilde-up": ("read_file", {"path": "~/../../../../../home/ubuntu/ops/x/test.json"}),
    "prose-abs-bench": ("write_file", {"path": "wiki/p.md", "content": "see %s/test.json\n" % SRC_BENCH}),
    "script-rel": ("write_file", {"path": "scratch/p.py", "content": "open('../other/test.json')\n"}),
}
R2_12_MISSES = {
    "tilde-user": ("terminal", {"command": "cat ~ubuntu/ops/hermes-wikiskill-src/benches/house-report/test.json"}),
    "tilde-user-ls": ("terminal", {"command": "ls ~ubuntu/ops"}),
    "python-join-dotdot": ("terminal", {"command":
                           "python3 -c \"import os;print(open(os.path.join('..','other-ws','tasks.json')).read())\""}),
    "python-listdir-dotdot": ("terminal", {"command": "python3 -c \"import os;print(os.listdir('..'))\""}),
    "python-walk-root": ("terminal", {"command":
                         "python3 -c \"import os\nfor r,d,f in os.walk('/'):\n  [print(open(os.path.join(r,x)).read()) for x in f if x=='test.json']\""}),
    "backslash-abs": ("terminal", {"command": "cat \\/home/ubuntu/ops/x/test.json"}),
}


def _mk(tmp_path):
    ws = str(tmp_path / "ws")
    os.makedirs(os.path.join(ws, "wiki"))
    return ws


def _flagged(tmp_path, outside, t, a):
    a = json.loads(json.dumps(a).replace("OUT", outside))
    return heldout.detect_outside(_calls_session([(t, a)]), _mk(tmp_path))


@pytest.mark.parametrize("case", sorted(TRUE_POS))
def test_v03_true_positive_flagged(tmp_path, outside, case):
    assert _flagged(tmp_path, outside, *TRUE_POS[case]), case


@R2_12
@pytest.mark.parametrize("case", sorted(R2_12_MISSES))
def test_v03_r2_12_known_miss(tmp_path, outside, case):
    assert _flagged(tmp_path, outside, *R2_12_MISSES[case]), case


def test_v03_present_false_still_flags_outside(tmp_path, answers, monkeypatch, outside):
    ws = _ws(tmp_path, answers)
    assert not heldout.heldout_present(ws)
    _role_calls(monkeypatch, [_read_outside(outside)])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert h["r_val"] is None and "out-of-workspace access" in h["proposal"], h


def test_v03_bench_dir_read_voids_via_evolve(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [("proposer", "terminal", {"command": "cat %s/test.json" % SRC_BENCH})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert h["r_val"] is None and "out-of-workspace access" in h["proposal"], h


def test_v03_eval_path_with_present(tmp_path, answers, monkeypatch):
    from test_t7_heldout import _evaluated_ws
    ws, _ts, _s = _evaluated_ws(tmp_path, answers)
    _role_calls(monkeypatch, [("maintainer", "terminal", {"command": "cat eval/t7/summary.json"})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert h["r_val"] is None and "read eval data" in h["proposal"], h


@R2_12
def test_v03_fp_terminal_grep_of_train_trace_with_escape_path(tmp_path):
    ws = _mk(tmp_path)
    line = '{"role":"tool","content":"ls: cannot access \'/home/ubuntu/ops/hws-acceptance/x\': Permission denied"}'
    via_terminal = _with_results([("terminal", {"command": "grep -n denied raw/traces/iter-01/train/tr-0/transcript.jsonl"},
                                   json.dumps({"output": line + "\n"}))])
    via_read = _with_results([("read_file", {"path": "raw/traces/iter-01/train/tr-0/transcript.jsonl"},
                               json.dumps({"content": line}))])
    assert heldout.detect_outside(via_terminal, ws) == [] and heldout.detect_outside(via_read, ws) == []


@R2_12
def test_v03_fp_git_config_in_wiki_voids(tmp_path, answers, monkeypatch):
    ws = _ws(tmp_path, answers)
    cfg = open(os.path.join(ws, "wiki", ".git", "config")).read() + "[user]\n\temail = m@x\n\tname = m\n"
    _role_calls(monkeypatch, [("maintainer", "write_file", {"path": "wiki/.git/config", "content": cfg})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    assert h["r_val"] is not None


# ============================================================== T10c: HEAD export hardening (R2-05)
def _git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _killed_role(ws, role="proposer"):
    st = gating.load_state(ws)
    st["role_running"] = {"role": role, "active_head": gating.active_head(ws)}
    gating.save_state(ws, st)
    return st["role_running"]["active_head"]


def test_t10c_eval_measures_pre_role_commit_when_killed_role_moved_head(tmp_path, answers, capsys, private_tmp):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    gated = _killed_role(ws)
    add_skill(ws, "sneaky", body="role commit")  # the killed role's own `git commit` in skills/active
    assert gating.active_head(ws) != gated
    s = evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="moved")
    assert s["active_commit"] == gated and "sneaky" not in s["skill_names"]
    assert not os.path.exists(os.path.join(ws, "eval", "moved", "skills", "sneaky"))
    assert "HEAD moved" in s["ungated_tree_ignored"] and "HEAD moved" in capsys.readouterr().err
    assert _hws_tmp(private_tmp) == []  # the hws-evalhead- export is removed


def test_t10c_eval_refuses_tampered_attributes(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    _killed_role(ws)
    info = os.path.join(ws, "skills", "active", ".git", "info")
    os.makedirs(info, exist_ok=True)
    with open(os.path.join(info, "attributes"), "w") as f:
        f.write("* export-ignore\n")
    with pytest.raises(HwsError, match="info/attributes"):
        evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="attr")
    assert not os.path.exists(os.path.join(ws, "eval", "attr"))


def test_t10c_replace_ref_ignored_and_refused(tmp_path, answers):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    act = os.path.join(ws, "skills", "active")
    gated = gating.active_head(ws)
    add_skill(ws, "replaced", body="ungated via replace ref")
    fake = gating.active_head(ws)
    _git(act, "reset", "-q", "--hard", gated)
    _git(act, "replace", gated, fake)  # every replace-honouring git call now sees `fake` as `gated`
    from hermes_wikiskill import gitutil
    out = str(tmp_path / "export")
    os.makedirs(out)
    assert not any("replaced" in r for r in gitutil.export_tree(act, gated, out))  # GIT_NO_REPLACE_OBJECTS
    assert any(b.startswith("refs/replace/") for b in gitutil.unsafe_metadata(act))
    with pytest.raises(HwsError, match="refs/replace"):
        evaluate.evaluate(ws, _test_file(tmp_path), skills="active", tag="repl")
    with pytest.raises(HwsError, match="git metadata modified outside the harness"):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)  # resume static check refuses too


def test_t10c_export_tree_without_tarfile_filter(tmp_path, monkeypatch):
    import tarfile
    from hermes_wikiskill import gitutil
    from hermes_wikiskill.errors import UnsafePathError

    def no_filter(self, *a, **kw):  # Python 3.10.0-3.10.11: no `filter` kwarg
        if "filter" in kw:
            raise TypeError("extractall() got an unexpected keyword argument 'filter'")
        raise AssertionError("export_tree must not depend on tarfile")
    monkeypatch.setattr(tarfile.TarFile, "extractall", no_filter)
    repo = str(tmp_path / "r")
    os.makedirs(os.path.join(repo, "s", "sub"))
    _git(repo, "init", "-q")
    open(os.path.join(repo, "s", "SKILL.md"), "w").write("---\nname: s\n---\nbody\n")
    open(os.path.join(repo, "s", "sub", "run.sh"), "w").write("#!/bin/sh\necho hi\n")
    os.chmod(os.path.join(repo, "s", "sub", "run.sh"), 0o755)
    open(os.path.join(repo, "s", "bin.dat"), "wb").write(bytes(range(256)) + b"\n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c1")
    open(os.path.join(repo, "s", "untracked.md"), "w").write("working tree only\n")
    out = str(tmp_path / "out")
    os.makedirs(out)
    got = gitutil.export_tree(repo, _git(repo, "rev-parse", "HEAD"), out)
    assert sorted(got) == ["s/SKILL.md", "s/bin.dat", "s/sub/run.sh"]
    assert open(os.path.join(out, "s", "bin.dat"), "rb").read() == bytes(range(256)) + b"\n\n"
    assert os.stat(os.path.join(out, "s", "sub", "run.sh")).st_mode & 0o111
    assert not os.path.exists(os.path.join(out, "s", "untracked.md"))
    os.symlink("/etc/passwd", os.path.join(repo, "s", "link"))  # an unsafe member: refused, never followed
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "c2")
    out2 = str(tmp_path / "out2")
    os.makedirs(out2)
    with pytest.raises(UnsafePathError, match="s/link"):
        gitutil.export_tree(repo, _git(repo, "rev-parse", "HEAD"), out2)
    assert not os.path.lexists(os.path.join(out2, "s", "link"))


# ============================================================== T10d: role start on the filesystem clock
def test_t10d_started_ns_uses_filesystem_clock(tmp_path, answers, monkeypatch):
    """Linux file times come from the coarse clock (<= one jiffy behind time.time_ns()): a file the
    role writes right after start must never look older than the recorded start. Simulated on any OS
    by a harness clock 50 ms AHEAD of the filesystem clock (qa QA-REPORT Round 6 B4 — host symptom on
    87afefc: 'none changed since it started')."""
    import time
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    real = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: real() + 50_000_000)  # stays ahead through the resume
    harness._mark_role(ws, "proposer", gating.active_head(ws), None, 2)
    p = os.path.join(ws, "leak.md")
    with open(p, "w") as f:
        f.write("written by the role in its first ms\n")
    started = gating.load_state(ws)["role_running"]["started_ns"]
    assert started <= os.lstat(p).st_ctime_ns
    with pytest.raises(HwsError, match="cannot be verified: leak.md"):  # no snapshot → fail closed, names it
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert "runs/.role-stamp" not in json.dumps(harness._q_walk(ws, "runs/x", "")[0])


def test_t10e_non_regular_role_stamp_is_a_clean_operator_error(tmp_path, answers, private_tmp):
    """QA-F24: a directory at runs/.role-stamp → HwsError naming it (no traceback, no role started,
    the entry and its content kept, no snapshot dir left — checked in a private tmp, QA-F25)."""
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    d = os.path.join(ws, "runs", ".role-stamp")
    os.remove(d)  # iteration 1's regular stamp
    os.makedirs(d)
    open(os.path.join(d, "keep.txt"), "w").write("operator data\n")
    assert _hws_tmp(private_tmp) == []
    with pytest.raises(HwsError, match=re.escape(os.path.join("runs", ".role-stamp"))):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert open(os.path.join(d, "keep.txt")).read() == "operator data\n"
    assert "role_running" not in gating.load_state(ws)
    assert not os.path.exists(os.path.join(ws, "runs", "maintain-02", "run.json"))
    assert _hws_tmp(private_tmp) == []
    r = hws("maintain", ws, "--iter", "2", "--json")
    assert r.returncode != 0 and "Traceback" not in r.stderr and ".role-stamp" in (r.stdout + r.stderr), r.stderr
    assert _hws_tmp(private_tmp) == []


# ============================================================== T10g: no out-of-ws path in role-readable ws records
def _abs_forms(p):
    return {p, os.path.realpath(p)}


def _ws_records_with(ws, needles):
    """Files under the ws (outside eval/, .git dirs and the roles' own session exports) containing a needle."""
    out = []
    for dp, dn, fn in os.walk(ws):
        rel = os.path.relpath(dp, ws)
        dn[:] = [d for d in dn if d != ".git" and not (rel == "." and d == "eval")]
        for f in fn:
            if f == "session.jsonl":
                continue
            p = os.path.join(dp, f)
            if os.path.islink(p):
                continue
            b = open(p, "rb").read()
            if any(n.encode() in b for n in needles):
                out.append(os.path.relpath(p, ws))
    return out


def test_t10g_f26_role_reading_its_own_state_during_the_run_is_not_voided(tmp_path, answers, monkeypatch, private_tmp):
    """qa live shape (tap 20260929_104943_ae96af msg 35): the proposer `cat`s runs/state.json DURING its run."""
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [("proposer", "terminal", {"command": "cat runs/state.json"})])
    h = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)["history"][-1]
    sess = open(os.path.join(ws, "runs", "propose-01", "session.jsonl")).read()
    assert "role_running" in sess and "hws-rolesnap-" in sess  # the role really saw the in-run record
    assert private_tmp not in sess and os.path.realpath(private_tmp) not in sess
    assert h["r_val"] is not None and "contaminated" not in h["proposal"], h


@pytest.mark.parametrize("ref", ["../x", "hws-rolesnap-link", "other-abc123", "/elsewhere/hws-rolesnap-x"])
def test_t10g_f26_tampered_snapshot_ref_fails_closed(tmp_path, answers, private_tmp, ref):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    assert os.path.dirname(snap) == private_tmp
    os.symlink(snap, os.path.join(private_tmp, "hws-rolesnap-link"))  # a link is never accepted
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)
    st = gating.load_state(ws)
    assert st["role_running"]["snapshot"] == os.path.basename(snap)  # never the absolute path
    st["role_running"]["snapshot"] = ref
    gating.save_state(ws, st)
    open(os.path.join(ws, "leak.md"), "w").write("x\n")
    with pytest.raises(HwsError, match="no pre-role snapshot"):
        harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert os.path.isdir(snap)  # an unresolved ref is never followed / removed
    shutil.rmtree(snap)


def test_t10g_f26_valid_basename_resolves_and_quarantines(tmp_path, answers, private_tmp):
    ws = _ws(tmp_path, answers)
    harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    snap = harness._ws_snapshot(ws, "maintainer", 2)
    harness._mark_role(ws, "maintainer", gating.active_head(ws), snap, 2)
    open(os.path.join(ws, "leak.md"), "w").write("x\n")
    harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    assert not os.path.exists(os.path.join(ws, "leak.md")) and not os.path.exists(snap)
    assert _hws_tmp(private_tmp) == []


def test_t10g_f27_void_note_does_not_cascade(tmp_path, answers, monkeypatch, outside, private_tmp):
    """qa live shape (tap 20260929_105153_e2ff74): after a genuine F8 void, the next maintainer reads and
    patches wiki/log.md + skill-impact.md and cats the state + the voided run's confinement.json."""
    ws = _ws(tmp_path, answers)
    _role_calls(monkeypatch, [_read_outside(outside, "proposer")])
    s1 = harness.evolve(ws, iters=1, verbose=False, no_early_stop=True)
    assert "out-of-workspace access" in s1["history"][-1]["proposal"]
    needles = _abs_forms(outside) | _abs_forms(private_tmp)
    assert _ws_records_with(ws, needles) == []  # grep guard after the void
    _clear(monkeypatch)
    _role_calls(monkeypatch, [
        ("maintainer", "read_file", {"path": "wiki/log.md"}),
        ("maintainer", "read_file", {"path": "wiki/skill-impact.md"}),
        ("maintainer", "patch", {"path": "wiki/skill-impact.md", "old_string": "### iter-01", "new_string": "### iter-01"}),
        ("maintainer", "terminal", {"command": "cat wiki/log.md"}),
        ("maintainer", "terminal", {"command": "cat wiki/skill-impact.md"}),
        ("maintainer", "terminal", {"command": "cat runs/state.json"}),
        ("maintainer", "terminal", {"command": "cat runs/propose-01/confinement.json"})])
    s2 = harness.evolve(ws, iters=2, verbose=False, no_early_stop=True)
    h = s2["history"][-1]
    sess = open(os.path.join(ws, "runs", "maintain-02", "session.jsonl")).read()
    assert "out-of-workspace access" in sess  # the maintainer really read the void note
    assert h["iter"] == 2 and h["r_val"] is not None and "contaminated" not in h["proposal"], h
    assert _ws_records_with(ws, needles) == []
