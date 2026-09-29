"""D5: workspace profiles hold only the model provider's key (0600), refreshed every run,
key-less config, no auth.json, no messaging tokens; nothing secret is printed."""

import os
import stat

from conftest import fw_ws, hws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import credentials, gating
from hermes_wikiskill.backends import hermes


def _home(ws, impl):
    return os.path.join(ws, ".hermes-home")


@impls("D5: upstream copies the whole .env (bot tokens, all keys), auth.json and inline api_key")
def test_d05_profile_has_only_provider_credentials(impl, tmp_path):
    ws = fw_ws(tmp_path) if impl == "framework" else (import_upstream() and up_ws(tmp_path))
    home = _home(ws, impl)
    if impl == "framework":  # R1-17: the ws home keeps no key; every run writes a fresh temp home
        assert not os.path.exists(os.path.join(home, ".env"))
        home = str(tmp_path / "run-home")
        hermes.refresh_profile(ws, home)
    env = credentials.read_dotenv(os.path.join(home, ".env"))
    assert set(env) == {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"}
    assert not os.path.exists(os.path.join(home, "auth.json"))
    cfg = open(os.path.join(home, "config.yaml")).read()
    assert "api_key" not in cfg and "bot_token" not in cfg and "providers" not in cfg
    assert stat.S_IMODE(os.stat(os.path.join(home, ".env")).st_mode) == 0o600


@impls("D5: upstream never refreshes copied credentials (rotated keys go stale)")
def test_d05_credentials_refreshed_on_every_run(impl, tmp_path, fake_env, answers):
    ts = mini_tasks()
    answers(ts)
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        run = lambda: gating.run_task(ws, ts[0], 1, overwrite=True)  # noqa: E731
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        run = lambda: ug.run_task(ws, ts[0], 1, overwrite=True)  # noqa: E731
    src = fake_env["source"] / ".env"
    src.write_text(src.read_text().replace("fake-key-1", "fake-key-2"))
    run()
    import hashlib
    from conftest import fake_log
    chats = [r for r in fake_log(tmp_path) if r["argv"][:1] == ["chat"]]
    assert chats[-1].get("home_key_sha256") == hashlib.sha256(b"fake-key-2").hexdigest()


def test_d05_fallback_to_config_key_when_env_lacks_it(tmp_path, fake_env):
    (fake_env["source"] / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-bot\n")
    plan = credentials.plan_profile(str(fake_env["source"]))
    assert plan["env"] == {"DEEPSEEK_API_KEY": "fake-inline"}
    assert plan["config"] == {"model": {"provider": "deepseek", "default": "deepseek-v4-flash",
                                        "base_url": "https://api.deepseek.test/v1"}}


def test_d05_missing_key_reported_by_name_only(tmp_path, fake_env):
    (fake_env["source"] / ".env").write_text("")
    (fake_env["source"] / "config.yaml").write_text("model:\n  provider: openrouter\n")
    plan = credentials.plan_profile(str(fake_env["source"]))
    assert plan["missing"] == ["OPENROUTER_API_KEY"] and plan["env"] == {}


def test_d05_no_secret_values_in_cli_output_or_run_records(tmp_path, answers):
    import json
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    r = hws("run-task", ws, "tr-0", "--json")
    assert r.returncode == 0, r.stderr
    run_json = open(os.path.join(ws, "runs", "iter-01", "train", "tr-0", "run.json")).read()
    for secret in ("fake-key-1", "fake-bot", "fake-groq", "fake-inline", "fake-nous"):
        assert secret not in r.stdout + r.stderr + run_json
    assert json.loads(run_json)["credentials"]["env"] == ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"]


def test_d05_model_override_is_keyless(tmp_path):
    ws = fw_ws(tmp_path)
    hermes.save_ws_settings(ws, model="other-model")
    hermes.refresh_profile(ws)
    cfg = open(os.path.join(ws, ".hermes-home", "config.yaml")).read()
    assert "default: other-model" in cfg and "api_key" not in cfg
