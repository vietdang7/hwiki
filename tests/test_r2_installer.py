"""ORCH Round 2 (ui T12): installer bookkeeping and profile ownership.

- R2-02 [MAJOR] install-state.json v3 `profiles[]` merged across runs; uninstall.sh removes every
  owned profile and undoes every operator-skill receipt. End to end with the REAL install.sh /
  uninstall.sh AND the real hermes_wikiskill package (built offline by a no-requirements PEP 517
  backend) in a fake HOME: the fake ~/.hermes must be byte-identical after uninstall, in both orders.
- R2-06 marker written first (incomplete) / crashed shell repaired; R2-07 marker v2 bound to name +
  path (a --clone-all copy is foreign; a legacy marker counts only when install-state lists it);
  R2-08 receipts found via the install prefix, and no silent "done" when the skill has no receipt.
Never the live ~/.hermes: HOME is a tmp dir and conftest pins HWS_HERMES_ROOT/HWS_HOME.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import hws
from hermes_wikiskill import profiles, resources
from hermes_wikiskill.errors import HwsError, UsageError
from test_install_scripts import FRAMEWORK, SCRIPTS, Env, _ok, _tree

REAL_BACKEND = r'''
import base64, hashlib, os, re, zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def _version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return re.search(r'^version *= *"([^"]+)"', fh.read(), re.M).group(1)


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    version = _version()
    name = "hermes_wikiskill-%s-py3-none-any.whl" % version
    dist_info = "hermes_wikiskill-%s.dist-info" % version
    records = []
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as zf:
        def add(arcname, data):
            zf.writestr(arcname, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append("%s,sha256=%s,%d" % (arcname, digest, len(data)))
        pkg = os.path.join(ROOT, "hermes_wikiskill")
        for d, dirs, files in os.walk(pkg):  # the whole package, data/ included
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            for fn in sorted(files):
                if fn.endswith((".pyc", ".pyo")):
                    continue
                p = os.path.join(d, fn)
                with open(p, "rb") as fh:
                    add("hermes_wikiskill/" + os.path.relpath(p, pkg).replace(os.sep, "/"), fh.read())
        add(dist_info + "/METADATA", ("Metadata-Version: 2.1\nName: hermes-wikiskill\nVersion: %s\n" % version).encode())
        add(dist_info + "/WHEEL", b"Wheel-Version: 1.0\nGenerator: hws-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        add(dist_info + "/entry_points.txt",
            b"[console_scripts]\nhermes-wikiskill = hermes_wikiskill.cli:main\nhws = hermes_wikiskill.cli:main\n")
        records.append(dist_info + "/RECORD,,")
        zf.writestr(dist_info + "/RECORD", "\n".join(records) + "\n")
    return name
'''

DEFAULT_CONFIG = "model:\n  provider: deepseek\n  default: deepseek-v4-flash\n"
DEFAULT_ENV = "DEEPSEEK_API_KEY=fake-dflt\n" + "TELEGRAM_BOT_TOKEN=fake-bot-default\n"


class RealEnv(Env):
    """Env with the real package as the installed source and a lived-in fake ~/.hermes."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        src = root / "hermes-wikiskill-real"
        shutil.copytree(FRAMEWORK / "hermes_wikiskill", src / "hermes_wikiskill",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # as setup.py does for the wheel: the repo's skills/ ship as package data
        shutil.copytree(FRAMEWORK / "skills" / resources.OPERATOR_SKILL,
                        src / "hermes_wikiskill" / "data" / "skills" / resources.OPERATOR_SKILL)
        for name in SCRIPTS:
            shutil.copy2(FRAMEWORK / name, src / name)
        version = profiles.__version__
        (src / "pyproject.toml").write_text(
            '[build-system]\nrequires = []\nbuild-backend = "hws_test_backend"\nbackend-path = ["."]\n\n'
            f'[project]\nname = "hermes-wikiskill"\nversion = "{version}"\n')
        (src / "hws_test_backend.py").write_text(REAL_BACKEND)
        self.src = src
        h = self.home / ".hermes"
        (h / "config.yaml").write_text(DEFAULT_CONFIG)
        (h / ".env").write_text(DEFAULT_ENV)
        (h / "SOUL.md").write_text("owner soul\n")
        (h / "skills" / "owner-notes").mkdir(parents=True)  # the live default profile's own skills
        (h / "skills" / "owner-notes" / "SKILL.md").write_text("---\nname: owner-notes\n---\nmine\n")
        clip = h / "profiles" / "otherprofile"  # a live foreign profile (gateway, bot token, own skills)
        (clip / "skills" / "clip-tools").mkdir(parents=True)
        (clip / "config.yaml").write_text("model:\n  provider: deepseek\ntelegram:\n  enabled: true\n")
        (clip / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-bot-clip\n")
        (clip / "skills" / "clip-tools" / "SKILL.md").write_text("---\nname: clip-tools\n---\nx\n")

    def run(self, script, *args, extra_env=None, cwd=None):
        env = {"HWS_UNINSTALL_WAIT": "0", "HWS_TESTING": None, "HWS_PASS_ENV": None}
        env.update(extra_env or {})
        return super().run(script, *args, extra_env=env, cwd=cwd)

    def state(self) -> dict:
        return json.loads((self.prefix / "install-state.json").read_text())

    def hermes_tree(self) -> dict[str, str]:
        return _tree(self.home / ".hermes")


@pytest.fixture()
def renv(tmp_path: Path) -> RealEnv:
    return RealEnv(tmp_path)


def _bump_source(renv: RealEnv) -> None:
    """An upgrade: new source content -> the package is reinstalled."""
    (renv.src / "hermes_wikiskill" / "_upgrade_probe.py").write_text("UPGRADED = True\n")


def _assert_uninstalled(renv: RealEnv, before: dict[str, str]) -> None:
    after = renv.hermes_tree()
    assert after == before, {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
                             "changed": sorted(k for k in before if k in after and before[k] != after[k])}
    assert not (renv.prefix / "venv").exists() and not renv.link("hws").is_symlink()
    assert not (renv.prefix / "install-state.json").exists()


# ---------------------------------------------------------------- R2-02 [MAJOR] end to end, both orders


def test_r2_02_install_then_owner_default_then_upgrade_then_uninstall_is_byte_identical(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    assert renv.profile_dir.is_dir()
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    assert (renv.home / ".hermes" / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    _bump_source(renv)
    out = _ok(renv.run("install.sh", "--yes"))  # README "Upgrade": the latest --profile is wikiskill again
    assert "changes: package" in out
    st = renv.state()

    out = _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)  # the R2-02 symptom first: nothing of ours left in ~/.hermes
    assert "profile: wikiskill (owned)" in out and "profile: default (foreign)" in out
    assert st.get("schema") == 3 and st["profile"] == "wikiskill"
    assert {p["name"]: p["kind"] for p in st["profiles"]} == {"wikiskill": "owned", "default": "operator-skill"}
    [rec] = [p for p in st["profiles"] if p["name"] == "default"][0]["receipts"]
    assert Path(rec).parent == renv.prefix / "receipts"
    assert json.loads(Path(rec).read_text())["removed_at"]  # receipts kept (no --purge), marked undone


def test_r2_02_owner_default_first_then_install_then_uninstall_is_byte_identical(renv: RealEnv) -> None:
    # the owner already had an own copy named hermes-wikiskill in default: it must come back byte for byte
    own = renv.home / ".hermes" / "skills" / "hermes-wikiskill"
    own.mkdir()
    (own / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: the owner's own copy\n---\nmine\n")
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    assert "owner's own copy" not in (own / "SKILL.md").read_text()
    assert not renv.profile_dir.exists()
    _ok(renv.run("install.sh", "--yes"))  # README Upgrade / plain install afterwards
    assert renv.profile_dir.is_dir()
    st = renv.state()
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)
    assert {p["name"] for p in st["profiles"]} == {"default", "wikiskill"}
    assert list((renv.home / "ops" / "hermes-wikiskill-backups").iterdir())


def test_r2_02_uninstall_ignores_latest_profile_and_undoes_unlisted_receipts(renv: RealEnv) -> None:
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    _ok(renv.run("install.sh", "--yes"))
    st_path = renv.prefix / "install-state.json"
    st = renv.state()
    st["profiles"] = [p for p in st["profiles"] if p["name"] != "default"]  # a receipt the state lost
    st_path.write_text(json.dumps(st, sort_keys=True) + "\n")
    dry = _ok(renv.run("uninstall.sh", "--dry-run"))
    assert "would undo its operator skill: hws setup-profile --profile default --remove --i-am-owner" in dry
    assert "would remove profile dir" in dry and renv.hermes_tree() != before
    _ok(renv.run("uninstall.sh", "--yes", "--profile", "wikiskill"))
    _assert_uninstalled(renv, before)


def test_r2_02_changed_operator_skill_stops_uninstall_before_anything_is_removed(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    skill = renv.home / ".hermes" / "skills" / "hermes-wikiskill" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nowner edit\n")
    proc = renv.run("uninstall.sh", "--yes")
    assert proc.returncode == 1 and "changed since it was installed" in proc.stderr
    assert (renv.prefix / "venv").is_dir() and renv.profile_dir.is_dir() and skill.is_file()


def test_r2_02_state_is_merged_and_second_run_changes_nothing(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    st1 = renv.state()
    out = _ok(renv.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    assert "no changes" in out and renv.state() == st1
    assert "status" not in st1 and [p["name"] for p in st1["profiles"]] == ["wikiskill", "default"]


# ---------------------------------------------------------------- R2-07 legacy host profile + clone copy (real scripts)


def test_r2_07_legacy_host_install_upgrades_and_uninstalls(renv: RealEnv) -> None:
    """The host today: v2 install-state (only `profile`) + a legacy marker without name/path."""
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    st = renv.state()
    legacy_state = {k: st[k] for k in ("source_sha256", "version", "profile", "source_dir")}
    (renv.prefix / "install-state.json").write_text(json.dumps(legacy_state, sort_keys=True) + "\n")
    marker = renv.profile_dir / profiles.MARKER
    marker.write_text(json.dumps({"created_by": "hermes-wikiskill", "version": "0.2.0",
                                  "created": "2026-09-28T00:00:00Z"}) + "\n")
    _bump_source(renv)
    out = _ok(renv.run("install.sh", "--yes"))  # upgrade: the legacy profile is still ours
    assert "profile wikiskill created/refreshed" in out and "doctor" in out
    m = json.loads(marker.read_text())
    assert m["profile"] == "wikiskill" and m["status"] == "complete" and m["created"] == "2026-09-28T00:00:00Z"
    assert m["path"] == os.path.realpath(renv.profile_dir)
    assert {p["name"]: p["kind"] for p in renv.state()["profiles"]} == {"wikiskill": "owned"}
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)


def test_r2_07_clone_all_copy_is_foreign_and_survives_uninstall(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    copy = renv.home / ".hermes" / "profiles" / "wikiskill-copy"
    shutil.copytree(renv.profile_dir, copy, symlinks=True)  # what `hermes profile create --clone-all` does
    proc = renv.run("install.sh", "--yes", "--profile", "wikiskill-copy")
    assert proc.returncode == 1 and "belongs to profile 'wikiskill'" in (proc.stdout + proc.stderr + (
        renv.prefix / "install.log").read_text())
    assert "wikiskill-copy" not in [p["name"] for p in renv.state()["profiles"]]  # refused -> never listed
    _ok(renv.run("uninstall.sh", "--yes"))
    assert copy.is_dir() and (copy / ".env").is_file() and not renv.profile_dir.exists()


# ---------------------------------------------------------------- unit: ownership rules (fake root from conftest)


def _state(tmp_path, **st) -> None:
    home = tmp_path / "hws"
    home.mkdir(exist_ok=True)
    (home / "install-state.json").write_text(json.dumps(st))


def test_r2_07_marker_v2_is_bound_to_name_and_path(tmp_path) -> None:
    profiles.setup_profile("ops-a")
    m = profiles.read_marker("ops-a")
    assert m["created_by"] == "hermes-wikiskill" and m["profile"] == "ops-a" and m["status"] == "complete"
    assert m["path"] == os.path.realpath(profiles.profile_dir("ops-a"))
    assert profiles.is_ours("ops-a")
    shutil.copytree(profiles.profile_dir("ops-a"), profiles.profile_dir("ops-b"))
    assert profiles.ownership("ops-b")[0] == "foreign" and profiles.is_foreign("ops-b")
    with pytest.raises(UsageError, match="a copy"):
        profiles.setup_profile("ops-b")


def test_r2_07_legacy_marker_owned_only_when_install_state_lists_it(tmp_path) -> None:
    d = Path(profiles.profile_dir("legacy"))
    d.mkdir(parents=True)
    (d / profiles.MARKER).write_text(json.dumps({"created_by": "hermes-wikiskill", "version": "0.2.0"}))
    assert profiles.ownership("legacy")[0] == "foreign"
    _state(tmp_path, profile="legacy", source_sha256="x")  # v2 state of the host
    assert profiles.is_ours("legacy")
    _state(tmp_path, profile="other", profiles=[{"name": "legacy", "kind": "operator-skill"}])
    assert profiles.is_foreign("legacy")
    _state(tmp_path, profile="other", profiles=[{"name": "legacy", "kind": "owned"}])
    out = profiles.setup_profile("legacy")  # upgraded to v2 in place
    assert out["changed"] and profiles.read_marker("legacy")["profile"] == "legacy"


def test_r2_06_marker_written_first_so_a_crashed_setup_is_repaired(tmp_path, monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("crash mid-setup")
    with monkeypatch.context() as mp:
        mp.setattr(profiles, "_install_operator_skill", boom)
        with pytest.raises(RuntimeError):
            profiles.setup_profile("crashy")
    m = profiles.read_marker("crashy")
    assert m["status"] == "incomplete" and profiles.is_ours("crashy")
    out = profiles.setup_profile("crashy")  # repaired, not "use --i-am-owner"
    assert out["changed"] and profiles.read_marker("crashy")["status"] == "complete"
    assert (Path(profiles.skills_dir("crashy")) / resources.OPERATOR_SKILL / "SKILL.md").is_file()
    again = profiles.setup_profile("crashy")
    assert again["changed"] is False


def test_r2_06_markerless_framework_shell_is_repaired_only_when_listed(tmp_path) -> None:
    d = Path(profiles.profile_dir("shell"))
    for sub in profiles.SUBDIRS:
        (d / sub).mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n")
    (d / "SOUL.md").write_text(profiles.SOUL)
    assert profiles.is_foreign("shell")  # not listed: never adopted
    _state(tmp_path, profile="shell", source_sha256="x")  # listed, but the dir existed before (QA-F21)
    assert profiles.is_foreign("shell")
    _state(tmp_path, profiles=[{"name": "shell", "kind": "pending"}])
    assert profiles.is_foreign("shell")
    _state(tmp_path, profiles=[{"name": "shell", "kind": "pending", "absent_at_listing": True}])
    assert profiles.is_ours("shell")
    profiles.setup_profile("shell")
    assert profiles.read_marker("shell")["status"] == "complete"
    (d / "notes.txt").write_text("user file\n")  # anything else in it -> not a shell
    (d / profiles.MARKER).unlink()
    assert profiles.is_foreign("shell")


def test_r2_08_receipts_found_via_install_prefix_and_no_silent_done(tmp_path, monkeypatch) -> None:
    prefix = tmp_path / "custom-prefix"
    (prefix / "venv").mkdir(parents=True)
    (prefix / "install-state.json").write_text(json.dumps({"profile": "wikiskill"}))
    monkeypatch.delenv("HWS_HOME")
    monkeypatch.setattr(sys, "prefix", str(prefix / "venv"))
    assert profiles.hws_home() == str(prefix)
    root = tmp_path / "hermes-root"
    root.mkdir(exist_ok=True)
    (root / "config.yaml").write_text("x: 1\n")
    out = profiles.setup_profile("default", i_am_owner=True)
    assert Path(out["receipt"]).parent == prefix / "receipts"
    assert profiles.setup_profile("default", i_am_owner=True, remove=True)["changed"] is True
    assert not (root / "skills" / resources.OPERATOR_SKILL).exists()
    # our operator skill present, but no receipt where we look (e.g. wrong HWS_HOME): exit non-zero
    profiles.setup_profile("default", i_am_owner=True)
    monkeypatch.setenv("HWS_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(HwsError, match="HWS_HOME=<prefix>"):
        profiles.setup_profile("default", i_am_owner=True, remove=True)
    rc = hws("setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json",
             env={**os.environ, "HWS_HOME": str(tmp_path / "elsewhere")})
    assert rc.returncode != 0 and "no un-removed operator-skill receipt" in (rc.stdout + rc.stderr)
    assert (root / "skills" / resources.OPERATOR_SKILL).is_dir()  # nothing changed


# ---------------------------------------------------------------- T12c: QA-F21 [MAJOR], QA-F22, QA-F23


def _handmade(renv: RealEnv, name: str = "mini") -> Path:
    """An existing hand-made profile that looks like a framework shell: config.yaml, .env, empty dirs."""
    d = renv.home / ".hermes" / "profiles" / name
    for sub in ("sessions", "skills", "memories", "logs"):
        (d / sub).mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\n  default: my-own-model\n")
    (d / ".env").write_text("DEEPSEEK_API_KEY=fake-mini\n")
    return d


def test_qa_f21_existing_handmade_profile_is_never_adopted_without_owner(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    mini = _handmade(renv)
    before = renv.hermes_tree()
    proc = renv.run("install.sh", "--yes", "--profile", "mini")
    assert proc.returncode != 0 and "--i-am-owner" in proc.stdout + proc.stderr + (
        renv.prefix / "install.log").read_text()
    assert renv.hermes_tree() == before  # config.yaml/.env untouched, no marker/SOUL/skill written
    assert "mini" not in [p["name"] for p in renv.state()["profiles"]]
    _ok(renv.run("uninstall.sh", "--yes"))
    assert mini.is_dir() and (mini / "config.yaml").read_text().endswith("my-own-model\n")
    assert sorted(p.name for p in mini.iterdir()) == [".env", "config.yaml", "logs", "memories", "sessions", "skills"]


def test_qa_f21_handmade_profile_with_owner_flag_is_operator_skill_only_and_undone(renv: RealEnv) -> None:
    mini = _handmade(renv)
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes", "--profile", "mini", "--i-am-owner"))
    [e] = [p for p in renv.state()["profiles"] if p["name"] == "mini"]
    assert e["kind"] == "operator-skill" and e["receipts"] and not e.get("absent_at_listing")
    assert not (mini / profiles.MARKER).exists() and (mini / "config.yaml").read_text().endswith("my-own-model\n")
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)


def test_qa_f21_crash_repair_keeps_working_for_a_profile_absent_at_listing(renv: RealEnv) -> None:
    """keep-case: a setup that crashed before the marker left a shell of a profile that did not exist."""
    before = renv.hermes_tree()
    _ok(renv.run("install.sh", "--yes"))
    st = renv.state()
    st["profiles"].append({"name": "crashy", "kind": "pending", "path": None, "receipts": [], "absent_at_listing": True})
    (renv.prefix / "install-state.json").write_text(json.dumps(st, sort_keys=True) + "\n")
    shell = renv.home / ".hermes" / "profiles" / "crashy"
    for sub in ("sessions", "skills", "memories", "logs"):
        (shell / sub).mkdir(parents=True)
    (shell / "config.yaml").write_text("model:\n  provider: deepseek\n")
    _ok(renv.run("install.sh", "--yes", "--profile", "crashy"))  # repaired, no --i-am-owner needed
    m = json.loads((shell / profiles.MARKER).read_text())
    assert m["profile"] == "crashy" and m["status"] == "complete"
    [e] = [p for p in renv.state()["profiles"] if p["name"] == "crashy"]
    assert e["kind"] == "owned" and e["absent_at_listing"] is True  # a re-list never flips it
    _ok(renv.run("uninstall.sh", "--yes"))
    _assert_uninstalled(renv, before)


def test_qa_f22_remove_without_receipt_is_loud_for_any_copy_of_the_operator_skill(tmp_path) -> None:
    root = tmp_path / "hermes-root"
    old = root / "skills" / resources.OPERATOR_SKILL
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("---\nname: hermes-wikiskill\ndescription: an older operator skill\n---\nold\n")
    (root / "config.yaml").write_text("x: 1\n")
    with pytest.raises(HwsError, match="no un-removed operator-skill receipt"):
        profiles.setup_profile("default", i_am_owner=True, remove=True)
    assert (old / "SKILL.md").read_text().endswith("old\n")  # nothing changed
    rc = hws("setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json")
    assert rc.returncode != 0


def test_qa_f23_corrupt_install_state_is_refused_and_nothing_changes(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    _ok(renv.run("install.sh", "--yes", "--profile", "second"))
    st_path = renv.prefix / "install-state.json"
    st_path.write_text('{"profiles": [ broken')
    before_prefix = _tree(renv.prefix)
    before_hermes = renv.hermes_tree()
    for args in ((), ("--dry-run",), ("--profile", "third")):
        proc = renv.run("install.sh", "--yes", *args)
        assert proc.returncode != 0 and "not a valid JSON object" in proc.stderr and str(st_path) in proc.stderr
    assert _tree(renv.prefix) == before_prefix and renv.hermes_tree() == before_hermes
    assert st_path.read_text() == '{"profiles": [ broken'


# ---------------------------------------------------------------- T12d: T10g operator-only diagnostics


def test_t12d_uninstall_backs_up_diagnostics_and_purge_removes_them(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    diag = renv.prefix / "diagnostics" / "eff1-0123456789ab"
    diag.mkdir(parents=True, mode=0o700)
    (diag / "maintain-01-20260929T000000Z.json").write_text(json.dumps({"outside": [{"path": "/etc/example"}]}))
    dry = _ok(renv.run("uninstall.sh", "--dry-run", "--purge"))
    assert f"would remove {renv.prefix}/diagnostics" in dry
    out = _ok(renv.run("uninstall.sh", "--yes", "--purge"))
    assert not renv.prefix.exists() and "removed empty" in out
    [bk] = list((renv.home / "ops" / "hermes-wikiskill-backups").iterdir())
    copied = bk / "diagnostics" / "eff1-0123456789ab" / "maintain-01-20260929T000000Z.json"
    assert copied.is_file() and oct(bk.stat().st_mode & 0o777) == "0o700"


def test_t12d_uninstall_without_purge_keeps_and_lists_diagnostics(renv: RealEnv) -> None:
    _ok(renv.run("install.sh", "--yes"))
    (renv.prefix / "diagnostics" / "x").mkdir(parents=True)
    out = _ok(renv.run("uninstall.sh", "--yes"))
    assert (renv.prefix / "diagnostics" / "x").is_dir() and f"  {renv.prefix}/diagnostics" in out
    [bk] = list((renv.home / "ops" / "hermes-wikiskill-backups").iterdir())
    assert (bk / "diagnostics" / "x").is_dir()
