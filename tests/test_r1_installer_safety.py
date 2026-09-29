"""ORCH Round 1 (ui T9): profile ownership, owner-approved operator skill (backup + receipt +
undo), promote/demote owner rule and symlinks, doctor R1-15/R1-19. Fake ~/.hermes only
(HWS_HERMES_ROOT from conftest); the installer scripts are covered in test_install_scripts.py."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from conftest import add_skill, fw_ws, hws, mini_tasks
from hermes_wikiskill import doctor, gating, profiles, promote as pm, resources
from hermes_wikiskill.errors import HwsError, UsageError


def _files(root) -> dict[str, bytes]:
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in fn:
            p = os.path.join(dp, n)
            out[os.path.relpath(p, root)] = os.readlink(p).encode() if os.path.islink(p) else open(p, "rb").read()
    return out


def _otherprofile(tmp_path):
    """A live-looking foreign profile: messaging section + bot token + OAuth auth.json."""
    d = tmp_path / "hermes-root" / "profiles" / "otherprofile"
    (d / "skills" / "clip-tools").mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\ntelegram:\n  enabled: true\n")
    (d / ".env").write_text("DEEPSEEK_API_KEY=fake-clip" + "\n" + "TELEGRAM_BOT_TOKEN=fake-bot-clip" + "\n")
    (d / "auth.json").write_text(json.dumps({"providers": {"nous": {"token": "fake-oauth"}}}))
    (d / "SOUL.md").write_text("otherprofile soul\n")
    (d / "skills" / "clip-tools" / "SKILL.md").write_text("---\nname: clip-tools\n---\nx\n")
    return d


def _receipts(tmp_path):
    d = tmp_path / "hws" / "receipts"
    return sorted(d.glob("*-setup-*.json")) if d.is_dir() else []


# ---------------------------------------------------------------- R1-06 foreign profile = operator skill only


def test_r1_06_owner_flag_on_foreign_profile_installs_only_operator_skill(tmp_path):
    d = _otherprofile(tmp_path)
    before = _files(d)
    with pytest.raises(UsageError):
        profiles.setup_profile("otherprofile")  # no owner flag: refused, untouched
    assert _files(d) == before
    out = profiles.setup_profile("otherprofile", i_am_owner=True)
    assert out["changed"] and out["operator_skill_only"]
    after = _files(d)
    added = sorted(set(after) - set(before))
    assert added and all(p.startswith("skills/hermes-wikiskill/") for p in added)
    assert {k: v for k, v in after.items() if k in before} == before  # config/.env/auth.json/SOUL byte-identical
    assert not (d / profiles.MARKER).exists() and not profiles.is_ours("otherprofile")
    [rec] = _receipts(tmp_path)
    r = json.loads(rec.read_text())
    assert r["kind"] == "operator-skill" and r["profile"] == "otherprofile" and r["backup"] is None
    assert r["sha256_before"] is None and len(r["sha256_after"]) == 64
    assert "fake-bot-clip" not in json.dumps(out) + rec.read_text()
    # idempotent: no second receipt, nothing changes
    assert profiles.setup_profile("otherprofile", i_am_owner=True)["changed"] is False
    assert len(_receipts(tmp_path)) == 1 and _files(d) == after


def test_r1_06_cli_refuses_foreign_without_owner(tmp_path):
    d = _otherprofile(tmp_path)
    before = _files(d)
    r = hws("setup-profile", "--profile", "otherprofile", "--json")
    assert r.returncode == 2 and "--i-am-owner" in r.stderr
    assert _files(d) == before


# ---------------------------------------------------------------- R1-07 default: backup + receipt + undo


def test_r1_07_default_existing_skill_backed_up_and_restored_byte_identical(tmp_path):
    root = tmp_path / "hermes-root"
    own = root / "skills" / "hermes-wikiskill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: the owner's own copy\n---\nmine\n")
    (own / "notes.txt").write_text("keep\n")
    (root / "config.yaml").write_text("model:\n  provider: deepseek\n")
    before = _files(own)
    out = profiles.setup_profile("default", i_am_owner=True)
    assert out["changed"] and out["receipt"]
    r = json.loads(open(out["receipt"]).read())
    assert r["backup"] and os.path.isfile(r["backup"]) and r["sha256_before"]
    assert _files(own) != before  # replaced by the packaged operator skill
    rm = hws("setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json")
    assert rm.returncode == 0, rm.stderr
    assert json.loads(rm.stdout)["changed"] is True
    assert _files(own) == before  # byte-identical restore
    assert json.loads(open(out["receipt"]).read())["removed_at"]
    assert (root / "config.yaml").read_text() == "model:\n  provider: deepseek\n"
    # nothing left to undo
    again = profiles.setup_profile("default", i_am_owner=True, remove=True)
    assert again["changed"] is False and "nothing to undo" in again["note"]


def test_r1_07_default_fresh_install_then_remove_deletes_it(tmp_path):
    root = tmp_path / "hermes-root"
    root.mkdir()
    (root / "config.yaml").write_text("x: 1\n")
    profiles.setup_profile("default", i_am_owner=True)
    assert (root / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    out = profiles.setup_profile("default", i_am_owner=True, remove=True)
    assert out["changed"] and not (root / "skills" / "hermes-wikiskill").exists()
    assert sorted(os.listdir(root)) == ["config.yaml"]  # R3 (ui T15): the skills/ dir it created is gone too


def test_r1_07_remove_refuses_a_changed_skill_exit_4(tmp_path):
    root = tmp_path / "hermes-root"
    root.mkdir()
    profiles.setup_profile("default", i_am_owner=True)
    skill = root / "skills" / "hermes-wikiskill" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nowner edit\n")
    before = _files(root)
    r = hws("setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json")
    assert r.returncode == 4 and "changed since" in r.stderr
    assert _files(root) == before
    assert not json.loads(_receipts(tmp_path)[0].read_text()).get("removed_at")


def test_r1_07_remove_undoes_a_chain_of_upgrades(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    root.mkdir()
    profiles.setup_profile("default", i_am_owner=True)
    newer = tmp_path / "newer-skill"
    import shutil
    shutil.copytree(resources.operator_skill_dir(), newer)
    (newer / "SKILL.md").write_text((newer / "SKILL.md").read_text() + "\nv2\n")
    monkeypatch.setattr(resources, "operator_skill_dir", lambda: str(newer))
    assert profiles.setup_profile("default", i_am_owner=True)["changed"]
    assert len(_receipts(tmp_path)) == 2
    out = profiles.setup_profile("default", i_am_owner=True, remove=True)
    assert out["changed"] and len(out["actions"]) == 2
    assert not (root / "skills" / "hermes-wikiskill").exists()


def test_r1_07_remove_rules(tmp_path):
    profiles.setup_profile("wikiskill")
    with pytest.raises(UsageError):  # framework-owned: uninstall.sh removes it whole
        profiles.setup_profile("wikiskill", remove=True, i_am_owner=True)
    _otherprofile(tmp_path)
    with pytest.raises(UsageError):  # foreign: owner flag required
        profiles.setup_profile("otherprofile", remove=True)
    out = profiles.setup_profile("otherprofile", remove=True, i_am_owner=True)
    assert out["changed"] is False and "nothing to undo" in out["note"]


def test_r1_07_doctor_ok_for_owner_installed_foreign_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("HWS_PASS_ENV", raising=False)
    _otherprofile(tmp_path)
    profiles.setup_profile("otherprofile", i_am_owner=True)
    out = doctor.run_checks("otherprofile")
    by = {c["id"]: c for c in out["checks"]}
    assert by["profile"]["status"] == "ok" and "foreign" in by["profile"]["detail"]
    assert by["profile-credentials"]["status"] == "ok" and by["operator-skill"]["status"] == "ok"
    assert "fake-bot-clip" not in json.dumps(out)


# ---------------------------------------------------------------- R1-21 / R1-16 promote + demote


def _git(ws, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=gating.active_dir(ws), check=True,
                          env=env, capture_output=True, text=True).stdout.strip()


def _accepted(tmp_path, link=False):
    ws = fw_ws(tmp_path, mini_tasks())
    d = add_skill(ws, commit=not link)
    if link:
        os.symlink(str(tmp_path / "source-home" / ".env"), os.path.join(d, "creds.txt"))
        _git(ws, "add", "-A")
        _git(ws, "commit", "-qm", "add house-report")
    state = gating.load_state(ws)
    state["history"] = [{"iter": 1, "accepted": True, "r_val": 1.0, "active_commit": _git(ws, "rev-parse", "HEAD")}]
    state["r_best"] = 1.0
    gating.save_state(ws, state)
    return ws


def _live(tmp_path, name):
    d = tmp_path / "hermes-root" / "profiles" / name
    (d / "skills").mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n")
    return d


def test_r1_21_promote_and_demote_need_owner_for_foreign_profiles(tmp_path):
    ws = _accepted(tmp_path)
    d = _live(tmp_path, "editor")
    with pytest.raises(UsageError):
        pm.promote(ws, "editor")
    assert pm.promote(ws, "editor", dry_run=True)["dry_run"] is True  # the owner may review the plan first
    assert os.listdir(d / "skills") == [] and not (tmp_path / "hws" / "receipts").exists()
    r = pm.promote(ws, "editor", i_am_owner=True)
    assert (d / "skills" / "house-report" / "SKILL.md").is_file()
    with pytest.raises(UsageError):
        pm.demote("editor", last=True)
    assert pm.demote("editor", last=True, i_am_owner=True)["promote_receipt"] == r["id"]
    # hws-test-* and framework-owned profiles need no owner flag
    _live(tmp_path, "hws-test-x")
    pm.promote(ws, "hws-test-x")
    profiles.setup_profile("wikiskill")
    pm.promote(ws, "wikiskill")
    assert pm.needs_owner("default") and pm.needs_owner("editor") and not pm.needs_owner("wikiskill")


def test_r1_20_documented_default_undo_works(tmp_path):
    ws = _accepted(tmp_path)
    root = tmp_path / "hermes-root"
    (root / "skills").mkdir(parents=True)
    (root / "config.yaml").write_text("model:\n  provider: deepseek\n")
    pm.promote(ws, "default", i_am_owner=True)
    r = hws("demote", "--profile", "default", "--last", "--i-am-owner", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["identical"] is True and not (root / "skills" / "house-report").exists()


def test_r1_16_promote_refuses_skills_with_symlinks(tmp_path):
    ws = _accepted(tmp_path, link=True)
    d = _live(tmp_path, "hws-test-l")
    with pytest.raises(HwsError, match="symlinks"):
        pm.promote(ws, "hws-test-l")
    assert os.listdir(d / "skills") == []
    assert not (tmp_path / "hws" / "receipts").exists() or not os.listdir(tmp_path / "hws" / "receipts")


# ---------------------------------------------------------------- doctor R1-15 / R1-19


@pytest.mark.parametrize("agent,status", [("{disabled_toolsets: [skills]}", "fail"), ("oops", "warn"),
                                          (["skills"], "fail"), ({"disabled_toolsets": []}, "ok")])
def test_r1_15_doctor_toolsets_non_mapping_agent(agent, status):
    assert doctor.check_toolsets({"agent": agent})[0] == status


def test_r1_15_doctor_inline_map_config(tmp_path, monkeypatch):
    monkeypatch.delenv("HWS_PASS_ENV", raising=False)
    profiles.setup_profile("wikiskill")
    cfg = tmp_path / "hermes-root" / "profiles" / "wikiskill" / "config.yaml"
    cfg.write_text("model:\n  provider: deepseek\nagent: {disabled_toolsets: [skills]}\n")
    by = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}
    assert by["profile-toolsets"]["status"] == "fail" and by["skills-visibility"]["status"] == "fail"


def test_r1_19_doctor_warns_when_pass_env_is_set(tmp_path, monkeypatch):
    profiles.setup_profile("wikiskill")
    monkeypatch.setenv("HWS_PASS_ENV", "FAKE_HERMES_*,SOME_NAME")
    by = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}
    assert by["pass-env"]["status"] == "warn" and "2 name(s)" in by["pass-env"]["detail"]
    assert "SOME_NAME" not in by["pass-env"]["detail"]
    monkeypatch.delenv("HWS_PASS_ENV")
    assert "pass-env" not in {c["id"] for c in doctor.run_checks("wikiskill")["checks"]}
