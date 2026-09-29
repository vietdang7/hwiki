"""D9: promote gate-ACCEPTED skills into a profile (backup + receipt + collision refusal +
owner rule) and demote them byte-identically."""

import json
import os

import pytest

from conftest import fw_ws, hws, mini_tasks
from hermes_wikiskill import harness, profiles, promote as pm
from hermes_wikiskill.errors import HwsError, UsageError


@pytest.fixture
def accepted_ws(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    harness.evolve(ws, iters=1, verbose=False)
    return ws


def _profile(tmp_path, name="hws-test-a"):
    d = tmp_path / "hermes-root" / "profiles" / name
    (d / "skills" / "existing").mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n")
    (d / "skills" / "existing" / "SKILL.md").write_text("---\nname: existing\n---\nkeep me\n")
    return d


def test_d09_promote_installs_accepted_skill_with_receipt(tmp_path, accepted_ws):
    d = _profile(tmp_path)
    r = pm.promote(accepted_ws, "hws-test-a")
    assert (d / "skills" / "house-report" / "SKILL.md").is_file()
    rec = json.load(open(os.path.join(tmp_path, "hws", "receipts", r["id"] + ".json")))
    assert rec["kind"] == "promote" and rec["gate"]["r_val"] == 1.0 and rec["gate"]["iter"] == 1
    assert rec["skills"][0]["name"] == "house-report" and len(rec["skills"][0]["sha256"]) == 64
    assert os.path.isfile(rec["backup"]) and rec["source_ws"] == accepted_ws
    for k in ("id", "ts", "profile", "skills_dir", "backup", "hermes_version"):
        assert k in rec


def test_d09_demote_restores_byte_identical(tmp_path, accepted_ws):
    d = _profile(tmp_path)
    before = pm.manifest(str(d / "skills"))
    pm.promote(accepted_ws, "hws-test-a")
    (d / "skills" / ".usage.json").write_text('{"house-report": 1}')  # Hermes skill_view bump
    out = pm.demote("hws-test-a", last=True)
    assert out["identical"] and pm.manifest(str(d / "skills")) == before
    with pytest.raises(HwsError):
        pm.demote("hws-test-a", last=True)  # already demoted


def test_d09_replace_and_demote_restores_previous_version(tmp_path, accepted_ws):
    d = _profile(tmp_path)
    old = d / "skills" / "house-report"
    old.mkdir()
    (old / "SKILL.md").write_text("---\nname: house-report\n---\nold version\n")
    before = pm.manifest(str(d / "skills"))
    with pytest.raises(HwsError, match="collision"):
        pm.promote(accepted_ws, "hws-test-a")
    r = pm.promote(accepted_ws, "hws-test-a", replace=True)
    assert r["skills"][0]["replaced"] is True and "FAKE-SOLVE" in (old / "SKILL.md").read_text()
    out = pm.demote("hws-test-a", receipt_id=r["id"])
    assert out["identical"] and pm.manifest(str(d / "skills")) == before


def test_d09_only_committed_accepted_content_is_promoted(tmp_path, accepted_ws):
    d = _profile(tmp_path)
    cand = os.path.join(accepted_ws, "skills", "active", "house-report", "SKILL.md")
    with open(cand, "a") as f:
        f.write("\nUNCOMMITTED EDIT\n")
    os.makedirs(os.path.join(accepted_ws, "skills", "active", "unaccepted"))
    open(os.path.join(accepted_ws, "skills", "active", "unaccepted", "SKILL.md"), "w").write("x")
    pm.promote(accepted_ws, "hws-test-a")
    assert "UNCOMMITTED" not in (d / "skills" / "house-report" / "SKILL.md").read_text()
    assert not (d / "skills" / "unaccepted").exists()
    with pytest.raises(HwsError, match="not gate-accepted"):
        pm.promote(accepted_ws, "hws-test-a", skills=["unaccepted"], replace=True)


def test_d09_refuses_without_accepted_gate_and_default_profile(tmp_path):
    _profile(tmp_path)
    ws = fw_ws(tmp_path)
    with pytest.raises(HwsError, match="no gate-ACCEPTED"):
        pm.promote(ws, "hws-test-a")
    with pytest.raises(UsageError):
        pm.promote(ws, "default")
    with pytest.raises(UsageError):
        pm.demote("default", last=True)


def test_d09_dry_run_is_pure(tmp_path, accepted_ws):
    from conftest import tree_hash
    d = _profile(tmp_path)
    h_prof, h_ws = tree_hash(str(d)), tree_hash(accepted_ws)
    out = pm.promote(accepted_ws, "hws-test-a", dry_run=True)
    assert out["dry_run"] and out["skills"][0]["name"] == "house-report"
    assert tree_hash(str(d)) == h_prof and tree_hash(accepted_ws) == h_ws
    assert not os.path.exists(os.path.join(tmp_path, "hws", "receipts"))


def test_d09_cli_promote_demote(tmp_path, accepted_ws):
    _profile(tmp_path)
    r = hws("promote", accepted_ws, "--profile", "hws-test-a", "--json")
    assert r.returncode == 0, r.stderr
    rid = json.loads(r.stdout)["id"]
    r2 = hws("demote", "--profile", "hws-test-a", "--receipt", rid, "--json")
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)["identical"] is True
    assert hws("promote", accepted_ws, "--profile", "default").returncode == 2


def test_d09_promoted_skill_visible_to_a_profile_session(tmp_path, accepted_ws):
    """Hermes indexes <profile>/skills: a session in the profile lists the promoted skill."""
    import subprocess
    from conftest import FAKE_HERMES
    from hermes_wikiskill import sessions
    profiles.setup_profile("hws-test-b")
    pm.promote(accepted_ws, "hws-test-b")
    home = profiles.profile_dir("hws-test-b")
    env = {**os.environ, "HERMES_HOME": home}
    p = subprocess.run([FAKE_HERMES, "chat", "-q", "hi", "-Q"], env=env, capture_output=True, text=True)
    sid = sessions.session_id_from_text(p.stderr)
    out = tmp_path / "s.jsonl"
    subprocess.run([FAKE_HERMES, "sessions", "export", "--format", "jsonl", "--session-id", sid,
                    str(out)], env=env, check=True)
    listed = sessions.available_skills(sessions.load(str(out))["system_prompt"])
    assert {"house-report", "hermes-wikiskill"} <= listed
