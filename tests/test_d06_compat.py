"""D6: Hermes flags are feature-detected under a scratch HERMES_HOME; old Hermes gets the
legacy argv (no unknown flags); no `hermes --version`; unusable Hermes -> exit 4."""

import pytest

from conftest import FAKE_HERMES, fake_log, fw_ws, hws, impls, import_upstream, mini_tasks, up_ws
from hermes_wikiskill import compat, gating
from hermes_wikiskill.errors import HermesIncompatibleError


def test_d06_modern_argv_uses_detected_flags(tmp_path):
    info = compat.detect(FAKE_HERMES)
    assert info.modern
    cmd, cwd = compat.build_chat_argv(info, query_file="/q", query_text="Q", toolsets="skills",
                                      max_turns=5, run_budget=60, model=None, workdir="/w")
    assert cmd[1:] == ["chat", "--query-file", "/q", "-Q", "--oneshot", "-t", "skills",
                       "--max-turns", "5", "--run-budget", "60", "--in", "/w"]
    assert cwd is None


@impls("D6: upstream passes --query-file/--oneshot/--run-budget/--in to an old Hermes (exit 2)")
def test_d06_old_hermes_legacy_fallback(impl, tmp_path, monkeypatch, answers):
    ts = mini_tasks()
    answers(ts)
    monkeypatch.setenv("FAKE_HERMES_FLAGS", "old")
    monkeypatch.setenv("FAKE_HERMES_ALWAYS_SOLVE", "1")
    if impl == "framework":
        ws = fw_ws(tmp_path, ts)
        r = gating.run_task(ws, ts[0], 1)
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path, ts)
        r = ug.run_task(ws, ts[0], 1)
    assert r["result"]["exit_code"] == 0 and r["score"] == 1.0


def test_d06_probe_runs_under_scratch_home_and_never_version(tmp_path):
    compat.detect(FAKE_HERMES)
    log = fake_log(tmp_path)
    helps = [r for r in log if r["argv"] == ["chat", "--help"]]
    assert helps and helps[0]["env"]["HERMES_HOME"].startswith("/tmp/hws-probe-")
    assert not any(a in ("--version", "-V") for r in log for a in r["argv"])


def test_d06_every_hermes_call_has_explicit_workspace_home(tmp_path, answers):
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    gating.run_task(ws, ts[0], 1)
    for r in fake_log(tmp_path):
        home = r["env"].get("HERMES_HOME", "")
        assert home.startswith(("/tmp/hws-probe-", ws, str(tmp_path / "rollouts"))), r


def test_d06_hermes_without_toolsets_flag_is_incompatible(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HERMES_FLAGS", "noskills")
    info = compat.detect(FAKE_HERMES)
    with pytest.raises(HermesIncompatibleError):
        compat.require_usable(info)
    ws = fw_ws(tmp_path)
    r = hws("run-task", ws, "tr-0")
    assert r.returncode == 4, r.stderr


def test_d06_missing_binary_exit_4(tmp_path):
    ws = fw_ws(tmp_path)
    r = hws("--hermes", str(tmp_path / "nope"), "run-task", ws, "tr-0")
    assert r.returncode == 4 and "not found" in r.stderr


def test_d06_version_from_package_metadata(tmp_path):
    import sys
    binary = tmp_path / "hermes"  # console-script shape: shebang = the install's python
    binary.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    binary.chmod(0o755)
    # the test interpreter has pytest installed: prove the metadata path works end to end
    monkey = compat.HERMES_DISTS
    try:
        compat.HERMES_DISTS = ("pytest",)
        v = compat.hermes_version(str(binary))
    finally:
        compat.HERMES_DISTS = monkey
    import importlib.metadata
    assert v == importlib.metadata.version("pytest")


def test_d06_version_through_bash_wrapper(tmp_path, monkeypatch):
    """Host shape: /usr/local/bin/hermes is `#!/bin/bash` + HERMES_VENV=... + exec ${HERMES_VENV}/bin/hermes."""
    import importlib.metadata
    import sys
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)  # the wrapper's venv python
    (venv / "bin" / "hermes").write_text("#!/bin/sh\nexit 0\n")
    wrapper = tmp_path / "hermes"
    wrapper.write_text(f'#!/bin/bash\nHERMES_VENV="{tmp_path}/venv"\nexec "${{HERMES_VENV}}/bin/hermes" "$@"\n')
    wrapper.chmod(0o755)
    cands = compat._python_candidates(str(wrapper))
    assert cands[0] == str(venv / "bin" / "python")
    # resolve with the real test interpreter (a symlinked venv python loses its site-packages on macOS)
    monkeypatch.setattr(compat, "_python_candidates", lambda b: [sys.executable])
    monkeypatch.setattr(compat, "HERMES_DISTS", ("pytest",))
    assert compat.hermes_version(str(wrapper)) == importlib.metadata.version("pytest")


def test_d06_version_unknown_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(compat, "FALLBACK_VENVS", ())
    b = tmp_path / "hermes"
    b.write_text("#!/bin/bash\nexec /nonexistent/hermes \"$@\"\n")
    assert compat.hermes_version(str(b)) is None


def test_d06_version_falls_back_to_hermes_agent_venv(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    b = tmp_path / "hermes"
    b.write_text("#!/bin/bash\nexec something-on-path \"$@\"\n")
    cands = compat._python_candidates(str(b))
    assert cands == [str(tmp_path / ".hermes/hermes-agent/venv/bin/python"),
                     str(tmp_path / ".hermes/hermes-agent/.venv/bin/python")]
