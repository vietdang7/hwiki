"""ORCH Round 3 (ui T15): uninstall is never permanently stuck (R3-02) + installer minors m4-m7.

REAL install.sh / uninstall.sh + the real package in a fake HOME (RealEnv of test_r2_installer):
- R3-02 [MAJOR] README Upgrade order (install -> owner `--profile default --i-am-owner` -> install.sh
  --yes), then the owner edits or deletes ~/.hermes/skills/hermes-wikiskill: a deleted skill (or one
  back to what the oldest receipt found) marks the receipt chain undone; an edited one is backed up
  (0700, recorded in the receipt) and the re-run finishes; exit 4 only where a human decides, with the
  exact command that unblocks uninstall.sh.
- m4 receipt before the skill; m5 pre-v3 install whose last run was the owner step; m6 stale pending
  entry; m7 receipt-less identical skill on a clone copy; modes + created parent dirs restored.
Never the live ~/.hermes: HOME is a tmp dir and conftest pins HWS_HERMES_ROOT/HWS_HOME.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import hws
from hermes_wikiskill import profiles, resources
from hermes_wikiskill.errors import HwsError
from test_install_scripts import _ok
from test_r2_installer import RealEnv, _assert_uninstalled, _bump_source


@pytest.fixture()
def renv(tmp_path: Path) -> RealEnv:
    return RealEnv(tmp_path)


def _skill(renv: RealEnv) -> Path:
    return renv.home / ".hermes" / "skills" / resources.OPERATOR_SKILL


def _upgrade_order(renv: RealEnv) -> None:
    """README Upgrade order: install -> owner step on default -> install.sh --yes (after an upgrade)."""
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    _bump_source(renv)
    _ok(renv.run("install.sh", "--yes"))


def _receipts(renv: RealEnv) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((renv.prefix / "receipts").glob("*-setup-default*.json"))]


def _modes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): stat.S_IMODE(os.lstat(p).st_mode) for p in [root, *root.rglob("*")]}


# ---------------------------------------------------------------- R3-02 [MAJOR]


def test_r3_02_deleted_operator_skill_marks_the_chain_undone_and_uninstall_completes(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _upgrade_order(renv)
    shutil.rmtree(_skill(renv))  # the owner deleted it
    out = _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)
    assert "already removed" in out
    [r] = _receipts(renv)
    assert r["removed_at"] and "already removed" in r["removed_reason"]


def test_r3_02_edited_operator_skill_is_backed_up_then_the_rerun_finishes(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _upgrade_order(renv)
    skill_md = _skill(renv) / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nowner edit via skill_manage\n")
    edited = skill_md.read_text()
    proc = renv.run("uninstall.sh", "--yes")
    assert proc.returncode == 1 and "changed since it was installed" in proc.stderr
    assert f"bash {renv.src / 'uninstall.sh'} --yes" in proc.stderr or "uninstall.sh --yes" in proc.stderr
    [r] = _receipts(renv)
    saved = Path(r["edited_backup"])
    assert str(saved) in proc.stderr and not r.get("removed_at")
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o700
    assert (saved / resources.OPERATOR_SKILL / "SKILL.md").read_text() == edited
    assert (renv.prefix / "venv").is_dir() and renv.profile_dir.is_dir() and skill_md.read_text() == edited
    out = _ok(renv.run("uninstall.sh", "--yes"))  # exactly what the message says: re-run
    _assert_uninstalled(renv, before)
    assert "profile: default (foreign)" in out
    [r] = _receipts(renv)
    assert r["removed_at"] and r["removed_reason"] == "removed"
    assert (saved / resources.OPERATOR_SKILL / "SKILL.md").read_text() == edited  # the backup stays


def test_r3_02_manual_remove_follows_the_same_two_steps(renv: RealEnv) -> None:
    _upgrade_order(renv)
    skill_md = _skill(renv) / "SKILL.md"
    skill_md.write_text(skill_md.read_text() + "\nowner edit\n")
    envb = {"HOME": str(renv.home), "PATH": f"{renv.hermes_bin}:/usr/bin:/bin", "LC_ALL": "C"}
    cmd = [str(renv.link("hws")), "setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json"]
    dry = subprocess.run(cmd + ["--dry-run"], env=envb, capture_output=True, text=True)
    assert dry.returncode == 0 and json.loads(dry.stdout)["edited"] is True
    assert not any("edited_backup" in r for r in _receipts(renv))  # a dry run writes nothing
    first = subprocess.run(cmd, env=envb, capture_output=True, text=True)
    assert first.returncode == 4 and "backed up to" in first.stderr and skill_md.is_file()
    second = subprocess.run(cmd, env=envb, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert not _skill(renv).exists()


def test_r3_02_owner_restored_own_copy_counts_as_undone(renv: RealEnv) -> None:
    own = _skill(renv)
    own.mkdir()
    (own / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: owner's own\n---\nmine\n")
    before = renv.hermes_tree()
    mine = (own / "SKILL.md").read_text()
    _upgrade_order(renv)
    shutil.rmtree(own)  # the owner put their own copy back by hand
    own.mkdir()
    (own / "SKILL.md").write_text(mine)
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)
    assert "what the oldest receipt found" in _receipts(renv)[0]["removed_reason"]


def test_r3_02_missing_backup_keeps_exit_4_and_the_stated_command_unblocks(renv: RealEnv) -> None:
    own = _skill(renv)
    own.mkdir()
    (own / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: owner's own\n---\nmine\n")
    _upgrade_order(renv)
    [r] = _receipts(renv)
    os.remove(r["backup"])  # the earlier copy cannot come back: a human decides
    proc = renv.run("uninstall.sh", "--yes")
    assert proc.returncode == 1 and "is missing" in proc.stderr and f"rm -rf {own}" in proc.stderr
    assert (renv.prefix / "venv").is_dir() and own.is_dir()
    shutil.rmtree(own)  # the stated command
    _ok(renv.run("uninstall.sh", "--yes"))
    assert not (renv.prefix / "venv").exists() and not own.exists()


# ---------------------------------------------------------------- m4: receipt before the skill


def test_m4_owner_step_writes_the_receipt_before_touching_the_skill(tmp_path, monkeypatch) -> None:
    root = tmp_path / "hermes-root"
    (root / "skills").mkdir(parents=True)
    (root / "config.yaml").write_text("x: 1\n")
    seen = []

    def crash(dst, dry_run, actions):  # the skill is written, then the process dies
        seen.extend(json.loads(p.read_text()) for p in (tmp_path / "hws" / "receipts").glob("*.json"))
        shutil.copytree(resources.operator_skill_dir(), dst)
        raise HwsError("simulated crash while installing the operator skill")

    with monkeypatch.context() as m:
        m.setattr(profiles, "_install_operator_skill", crash)
        with pytest.raises(HwsError, match="simulated crash"):
            profiles.setup_profile("default", i_am_owner=True)
    [r] = seen
    assert r["status"] == "incomplete" and r["sha256_after"] is None and r["sha256_before"] is None
    assert (root / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    out = profiles.setup_profile("default", i_am_owner=True, remove=True)  # uninstall's undo step
    assert out["changed"] and not (root / "skills" / "hermes-wikiskill").exists()


def test_m4_crash_before_the_receipt_leaves_default_untouched(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    sp = next((renv.prefix / "venv" / "lib").glob("python*/site-packages"))
    hook = sp / "zz_hws_crash.pth"
    hook.write_text("import os; exec(\"if os.environ.get('HWS_CRASH_AT'):\\n import hermes_wikiskill.profiles as _p\\n"
                    " def _boom(*a, **k):\\n  os._exit(137)\\n setattr(_p, os.environ['HWS_CRASH_AT'], _boom)\")\n")
    proc = renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner",
                    extra_env={"HWS_CRASH_AT": "atomic_write_json"})
    assert proc.returncode != 0 and not _skill(renv).exists()
    assert "default" not in [p["name"] for p in renv.state()["profiles"]]  # listed before, dropped: no receipt
    hook.unlink()
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)


# ---------------------------------------------------------------- m5: pre-v3 install, last run = owner step


def test_m5_pre_v3_state_after_the_owner_step_upgrades_and_uninstalls(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    st = renv.state()  # what a d70422c-era install left: v2 state naming only `default`, legacy marker
    legacy = {k: st[k] for k in ("source_sha256", "version", "source_dir")}
    (renv.prefix / "install-state.json").write_text(json.dumps({**legacy, "profile": "default"}) + "\n")
    (renv.profile_dir / profiles.MARKER).write_text(json.dumps(
        {"created_by": "hermes-wikiskill", "version": "0.2.0", "created": "2026-09-28T00:00:00Z"}) + "\n")
    _bump_source(renv)
    _ok(renv.run("install.sh", "--yes"))  # README Upgrade
    assert {p["name"]: p["kind"] for p in renv.state()["profiles"]}["wikiskill"] == "owned"
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)


def test_m5_pre_v3_state_uninstalls_without_an_upgrade_but_never_adopts_a_copy(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    st = renv.state()
    copy = renv.home / ".hermes" / "profiles" / "wikiskill-copy"
    legacy_marker = json.dumps({"created_by": "hermes-wikiskill", "version": "0.2.0", "created": "x"}) + "\n"
    (renv.profile_dir / profiles.MARKER).write_text(legacy_marker)
    shutil.copytree(renv.profile_dir, copy, symlinks=True)  # an old clone carries the legacy marker too
    (renv.prefix / "install-state.json").write_text(json.dumps(
        {**{k: st[k] for k in ("source_sha256", "version", "source_dir")}, "profile": "default"}) + "\n")
    out = _ok(renv.run("uninstall.sh", "--yes"))
    assert "profile: wikiskill (owned)" in out and copy.is_dir()
    shutil.rmtree(copy)
    _assert_uninstalled(renv, before)


# ---------------------------------------------------------------- m6: stale pending entry


def test_m6_failed_setup_leaves_no_pending_entry_to_adopt_a_later_handmade_profile(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    proc = renv.run("install.sh", "--yes", "--profile", "mini", "--source-profile", "mini")
    assert proc.returncode != 0 and not (renv.home / ".hermes" / "profiles" / "mini").exists()
    assert "mini" not in [p["name"] for p in renv.state()["profiles"]]
    d = renv.home / ".hermes" / "profiles" / "mini"
    for sub in ("sessions", "skills", "memories", "logs"):
        (d / sub).mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n  default: my-own\n")
    out = _ok(renv.run("uninstall.sh", "--yes"))
    assert "profile: mini (owned)" not in out and (d / "config.yaml").is_file()


# ---------------------------------------------------------------- m7: receipt-less identical skill


def test_m7_owner_flag_on_a_clone_with_an_identical_skill_is_not_listed_and_uninstall_completes(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    copy = renv.home / ".hermes" / "profiles" / "wikiskill-copy"
    shutil.copytree(renv.profile_dir, copy, symlinks=True)
    kept = {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    _ok(renv.run("install.sh", "--yes", "--profile", "wikiskill-copy", "--i-am-owner"))
    assert "wikiskill-copy" not in [p["name"] for p in renv.state()["profiles"]]
    _ok(renv.run("uninstall.sh", "--yes"))
    assert {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()} == kept and not renv.profile_dir.exists()


# ---------------------------------------------------------------- byte-identical undo: modes + created dirs


def test_undo_restores_a_group_writable_own_copy_with_its_modes(renv: RealEnv) -> None:
    own = _skill(renv)
    own.mkdir()
    (own / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: owner's own\n---\nmine\n")
    os.chmod(own, 0o775)
    os.chmod(own / "SKILL.md", 0o664)
    modes = _modes(own)
    before = renv.hermes_tree()
    _upgrade_order(renv)
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)
    assert _modes(own) == modes


@pytest.mark.parametrize("order", ["plain", "owner-then-upgrade"])
def test_undo_removes_the_parent_dirs_it_created_in_a_fresh_hermes_root(renv: RealEnv, order: str) -> None:
    h = renv.home / ".hermes"
    shutil.rmtree(h)
    h.mkdir()
    (h / "config.yaml").write_text("model:\n  provider: deepseek\n  default: deepseek-v4-flash\n")
    (h / ".env").write_text("DEEPSEEK_API_KEY=fake-dflt\n")
    os.chmod(h / ".env", 0o600)
    before = sorted(p.name for p in h.iterdir())
    if order == "plain":
        _ok(renv.run("install.sh", "--yes"))
    else:
        _upgrade_order(renv)
    _ok(renv.run("uninstall.sh", "--yes"))
    assert sorted(p.name for p in h.iterdir()) == before


def test_created_parent_dirs_stay_when_something_else_lives_there(tmp_path) -> None:
    root = tmp_path / "hermes-root"
    root.mkdir()
    profiles.setup_profile("default", i_am_owner=True)
    (root / "skills" / "owner-later").mkdir()  # the owner added a skill afterwards
    profiles.setup_profile("default", i_am_owner=True, remove=True)
    assert sorted(os.listdir(root / "skills")) == ["owner-later"]


def test_manual_remove_json_names_the_rerun_command(tmp_path) -> None:
    root = tmp_path / "hermes-root"
    root.mkdir()
    profiles.setup_profile("default", i_am_owner=True)
    skill = root / "skills" / "hermes-wikiskill" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nedit\n")
    r = hws("setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json")
    assert r.returncode == 4 and "re-run the same command" in r.stderr and "uninstall.sh --yes" in r.stderr
