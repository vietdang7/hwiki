"""setup-profile (file-based, key-less, idempotent, owner rules) and doctor (§3.1)."""

import json
import os
import stat

import pytest

from conftest import hws
from hermes_wikiskill import credentials, doctor, profiles
from hermes_wikiskill.errors import UsageError

IDS = ["python", "hermes-binary", "hermes-version", "hermes-flags", "profile", "profile-toolsets",
       "profile-credentials", "operator-skill", "framework-skills", "skills-visibility",
       "workspace-locks", "disk", "ram"]


def _snapshot(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for n in fn:
            p = os.path.join(dp, n)
            out[os.path.relpath(p, root)] = open(p, "rb").read() if not os.path.islink(p) else os.readlink(p)
    return out


def test_setup_profile_creates_minimal_keyless_profile(tmp_path):
    out = profiles.setup_profile("wikiskill")
    d = tmp_path / "hermes-root" / "profiles" / "wikiskill"
    assert out["changed"] and out["profile_dir"] == str(d)
    env = credentials.read_dotenv(str(d / ".env"))
    assert set(env) == {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}
    assert stat.S_IMODE(os.stat(d / ".env").st_mode) == 0o600
    cfg = (d / "config.yaml").read_text()
    assert "api_key" not in cfg and "telegram" not in cfg and "disabled_toolsets: []" in cfg
    assert not (d / "auth.json").exists()
    assert (d / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    assert (d / profiles.MARKER).is_file() and profiles.is_live("wikiskill")


def test_setup_profile_idempotent_and_writes_only_inside_profile(tmp_path):
    profiles.setup_profile("wikiskill")
    root = tmp_path / "hermes-root"
    before = _snapshot(root)
    out = profiles.setup_profile("wikiskill")
    assert out["changed"] is False and out["actions"] == []
    assert _snapshot(root) == before
    assert set(os.listdir(root)) == {"profiles"} and os.listdir(root / "profiles") == ["wikiskill"]


def test_setup_profile_dry_run_is_pure(tmp_path):
    out = profiles.setup_profile("wikiskill", dry_run=True)
    assert out["changed"] and out["dry_run"] and out["actions"]
    assert not (tmp_path / "hermes-root").exists()


def test_setup_profile_refreshes_rotated_key(tmp_path, fake_env):
    profiles.setup_profile("wikiskill")
    src = fake_env["source"] / ".env"
    src.write_text(src.read_text().replace("fake-key-1", "fake-key-9"))
    out = profiles.setup_profile("wikiskill")
    assert out["changed"] and any(".env" in a for a in out["actions"])
    env = credentials.read_dotenv(str(tmp_path / "hermes-root/profiles/wikiskill/.env"))
    assert env["DEEPSEEK_API_KEY"] == "fake-key-9"


def test_setup_profile_refuses_default_and_foreign_profiles(tmp_path):
    with pytest.raises(UsageError):
        profiles.setup_profile("default")
    foreign = tmp_path / "hermes-root" / "profiles" / "editor"
    foreign.mkdir(parents=True)
    (foreign / "config.yaml").write_text("model:\n  provider: x\n")
    with pytest.raises(UsageError):
        profiles.setup_profile("editor")
    assert (foreign / "config.yaml").read_text() == "model:\n  provider: x\n"
    r = hws("setup-profile", "--profile", "default")
    assert r.returncode == 2


def test_setup_profile_default_owner_installs_only_operator_skill(tmp_path):
    root = tmp_path / "hermes-root"
    root.mkdir()
    for n, t in (("config.yaml", "model:\n  api_key: fake-owner\n"), (".env", "X=1\n"), ("auth.json", "{}")):
        (root / n).write_text(t)
    before = {n: (root / n).read_text() for n in ("config.yaml", ".env", "auth.json")}
    out = profiles.setup_profile("default", i_am_owner=True)
    assert out["operator_skill_only"] and out["changed"]
    assert {n: (root / n).read_text() for n in before} == before
    assert (root / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    assert sorted(os.listdir(root)) == [".env", "auth.json", "config.yaml", "skills"]
    assert profiles.setup_profile("default", i_am_owner=True)["changed"] is False


def test_setup_profile_cli_json(tmp_path):
    r = hws("setup-profile", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["changed"] is True and d["profile"] == "wikiskill"
    assert json.loads(hws("setup-profile", "--json").stdout)["changed"] is False
    assert "fake-key" not in r.stdout + r.stderr


def test_doctor_all_ok_after_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("HWS_PASS_ENV", raising=False)  # set by conftest for the fake hermes; doctor warns on it (R1-19)
    profiles.setup_profile("wikiskill")
    out = doctor.run_checks("wikiskill")
    assert [c["id"] for c in out["checks"]] == IDS
    bad = [c for c in out["checks"] if c["status"] == "fail"]
    assert out["ok"] and not bad, bad
    assert "fake-key" not in json.dumps(out)


def test_doctor_cli_exit_codes(tmp_path):
    r = hws("doctor", "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["ok"] is False  # no profile yet
    assert hws("setup-profile").returncode == 0
    r = hws("doctor", "--json")
    assert r.returncode == 0, r.stdout


def test_d17_doctor_fails_when_config_disables_skills(tmp_path):
    profiles.setup_profile("wikiskill")
    cfg = tmp_path / "hermes-root/profiles/wikiskill/config.yaml"
    cfg.write_text(cfg.read_text().replace("disabled_toolsets: []", "disabled_toolsets:\n    - skills"))
    checks = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}
    assert checks["profile-toolsets"]["status"] == "fail"
    assert checks["skills-visibility"]["status"] == "fail"


def test_doctor_flags_extra_credentials(tmp_path):
    profiles.setup_profile("wikiskill")
    env = tmp_path / "hermes-root/profiles/wikiskill/.env"
    env.write_text(env.read_text() + "TELEGRAM_BOT_TOKEN=fake-bot\n")
    c = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}["profile-credentials"]
    assert c["status"] == "fail" and "TELEGRAM_BOT_TOKEN" in c["detail"] and "fake-bot" not in c["detail"]


def test_doctor_live_run_proves_operator_skill_visible(tmp_path):
    profiles.setup_profile("wikiskill")
    out = doctor.run_checks("wikiskill", live=True)
    live = out["checks"][-1]
    assert live["id"] == "live-run" and live["status"] == "ok", live


def test_doctor_reports_busy_workspace(tmp_path):
    from conftest import fw_ws
    from test_d14_lock import _hold
    root = tmp_path / "hws" / "workspaces"
    ws = fw_ws(root, name="busy")
    h = _hold(os.path.join(ws, ".hws.lock"))
    try:
        c = {c["id"]: c for c in doctor.run_checks("x")["checks"]}["workspace-locks"]
        assert "busy" in c["detail"] and str(h.pid) in c["detail"]
    finally:
        h.kill()


def test_doctor_reports_gateway_served_state(tmp_path):
    profiles.setup_profile("wikiskill")
    root = tmp_path / "hermes-root"
    (root / "gateway_state.json").write_text(json.dumps({"served_profiles": []}))
    c = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}["profile"]
    assert c["status"] == "ok" and "served_profiles=[]" in c["detail"]
    (root / "gateway_state.json").write_text(json.dumps({"served_profiles": ["wikiskill"]}))
    c = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}["profile"]
    assert c["status"] == "warn"


HERMES_POOL = {"version": 1, "providers": {}, "updated_at": "2026-09-28T19:26:04Z",
               "credential_pool": {"deepseek": [{"id": "c1", "label": "env", "auth_type": "api_key",
                                                 "priority": 0, "source": "env:DEEPSEEK_API_KEY",
                                                 "base_url": "https://api.deepseek.test/v1",
                                                 "request_count": 3, "secret_fingerprint": "ab12"}]}}


def test_hermes_generated_fingerprint_auth_json_is_ok_and_kept(tmp_path):
    """Host receipt 20260928_192604_b6d277: Hermes writes this on the first chat."""
    profiles.setup_profile("wikiskill")
    auth = tmp_path / "hermes-root/profiles/wikiskill/auth.json"
    auth.write_text(json.dumps(HERMES_POOL))
    c = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}["profile-credentials"]
    assert c["status"] == "ok", c
    assert profiles.setup_profile("wikiskill")["changed"] is False and auth.exists()


@pytest.mark.parametrize("bad", [
    {"providers": {"nous": {"refresh_token": "fake-rt"}}},
    {"credential_pool": {"openai": [{"id": "x", "secret_fingerprint": "f"}]}},
    {"credential_pool": {"deepseek": [{"id": "x", "api_key": "fake-raw"}]}},
])
def test_auth_json_with_tokens_fails_doctor_and_is_removed(tmp_path, bad):
    profiles.setup_profile("wikiskill")
    auth = tmp_path / "hermes-root/profiles/wikiskill/auth.json"
    auth.write_text(json.dumps(bad))
    c = {c["id"]: c for c in doctor.run_checks("wikiskill")["checks"]}["profile-credentials"]
    assert c["status"] == "fail" and "fake-" not in c["detail"]
    assert profiles.setup_profile("wikiskill")["changed"] is True and not auth.exists()
