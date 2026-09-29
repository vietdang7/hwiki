"""install.sh / uninstall.sh end-to-end under a fake HOME (offline).

The scripts run for real: bash, venv, pip, symlinks, tar. Only the outside
world is stubbed:
- `hermes` is a stub launcher inside a hand-made venv that carries
  `hermes-agent` 0.21.3 metadata, so the version lookup path is exercised;
- the package installed is a stub `hermes_wikiskill` (version/setup-profile/
  doctor per INTERFACE) built by an in-tree PEP 517 backend with no build
  requirements, so pip needs no index (PIP_NO_INDEX=1).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


FRAMEWORK = Path(__file__).resolve().parents[1]
SCRIPTS = ("install.sh", "uninstall.sh")
BAD_LOG = re.compile(r"ERROR|Traceback|unrecognized arguments")

STUB_BACKEND = r'''
import base64, hashlib, os, re, zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def _version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return re.search(r'^version *= *"([^"]+)"', fh.read(), re.M).group(1)


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    if os.environ.get("STUB_BUILD_LOG"):  # R1-24: record the pip cache dir pip ran with
        with open(os.environ["STUB_BUILD_LOG"], "a") as fh:
            fh.write("PIP_CACHE_DIR=%s\n" % os.environ.get("PIP_CACHE_DIR", ""))
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
        for fn in sorted(os.listdir(pkg)):
            if fn.endswith(".py"):
                with open(os.path.join(pkg, fn), "rb") as fh:
                    add("hermes_wikiskill/" + fn, fh.read())
        add(dist_info + "/METADATA", ("Metadata-Version: 2.1\nName: hermes-wikiskill\nVersion: %s\n" % version).encode())
        add(dist_info + "/WHEEL", b"Wheel-Version: 1.0\nGenerator: hws-stub\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        add(dist_info + "/entry_points.txt",
            b"[console_scripts]\nhermes-wikiskill = hermes_wikiskill.cli:main\nhws = hermes_wikiskill.cli:main\n")
        records.append(dist_info + "/RECORD,,")
        zf.writestr(dist_info + "/RECORD", "\n".join(records) + "\n")
    return name
'''

STUB_CLI = r'''
import json, os, sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    with open(os.environ["STUB_HWS_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "HWS_HOME": os.environ.get("HWS_HOME"),
                             "HERMES_BIN": os.environ.get("HERMES_BIN")}) + "\n")
    verb = argv[0] if argv else ""
    if verb == "version":
        print(json.dumps({"tool": "hermes-wikiskill", "version": "0.2.0"}))
        return 0
    if verb == "setup-profile" and "--remove" in argv:
        rc = int(os.environ.get("STUB_REMOVE_RC", "0"))
        if rc:
            print("error: stub remove failed", file=sys.stderr)
            return rc
        for fn in sorted(os.listdir(os.path.join(os.environ["HWS_HOME"], "receipts"))) \
                if os.path.isdir(os.path.join(os.environ["HWS_HOME"], "receipts")) else []:
            rp = os.path.join(os.environ["HWS_HOME"], "receipts", fn)
            with open(rp) as fh:
                r = json.load(fh)
            if r.get("profile") == argv[argv.index("--profile") + 1] and "--dry-run" not in argv:
                with open(rp, "w") as fh:
                    json.dump({**r, "removed_at": "stub"}, fh)
        print(json.dumps({"changed": True, "actions": ["stub remove"], "receipt": None}))
        return 0
    if verb == "setup-profile":  # mirrors INTERFACE §6: foreign profile (incl. default) -> operator skill only
        profile = argv[argv.index("--profile") + 1]
        root = os.path.join(os.path.expanduser("~"), ".hermes")
        pdir = root if profile == "default" else os.path.join(root, "profiles", profile)
        foreign = profile == "default" or (os.path.exists(pdir) and
                                           not os.path.exists(os.path.join(pdir, ".hws-profile.json")))
        skill = os.path.join(pdir, "skills", "hermes-wikiskill", "SKILL.md")
        changed = not os.path.exists(skill)
        if changed:
            os.makedirs(os.path.dirname(skill), exist_ok=True)
            with open(skill, "w") as fh:
                fh.write("---\nname: hermes-wikiskill\n---\n")
        if changed and not foreign:
            with open(os.path.join(pdir, "config.yaml"), "w") as fh:
                fh.write("model:\n  provider: deepseek\n")
            with open(os.path.join(pdir, ".env"), "w") as fh:
                fh.write("DEEPSEEK_API_KEY=fake-key\n")
            with open(os.path.join(pdir, ".hws-profile.json"), "w") as fh:
                fh.write('{"created_by": "hermes-wikiskill"}\n')
        receipt = None
        if changed and foreign:  # like the real owner step (R3 m7: no receipt -> not listed)
            rdir = os.path.join(os.environ["HWS_HOME"], "receipts")
            os.makedirs(rdir, exist_ok=True)
            receipt = os.path.join(rdir, "20260929T000000Z-setup-%s.json" % profile)
            with open(receipt, "w") as fh:
                json.dump({"kind": "operator-skill", "id": "20260929T000000Z-setup-%s" % profile,
                           "profile": profile}, fh)
        print(json.dumps({"changed": changed, "profile": profile, "profile_dir": pdir, "receipt": receipt,
                          **({"operator_skill_only": True} if foreign else {})}))
        return 0
    if verb == "doctor":
        fail = bool(os.environ.get("STUB_DOCTOR_FAIL"))
        print(json.dumps({"tool": "hermes-wikiskill", "version": "0.2.0", "ok": not fail,
                          "checks": [{"id": "python", "status": "ok", "detail": "stub"},
                                     {"id": "profile", "status": "fail" if fail else "ok", "detail": "stub"}]}))
        return 1 if fail else 0
    return 2
'''

FAKE_HERMES = r'''
import json, os, sys

with open(os.environ["FAKE_HERMES_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "HERMES_HOME": os.environ.get("HERMES_HOME")}) + "\n")
if "--version" in sys.argv or "-V" in sys.argv:
    sys.exit(99)
if sys.argv[1:3] == ["chat", "--help"]:
    if os.environ.get("FAKE_HERMES_FLAGS") == "old":
        print("usage: hermes chat [-q QUERY] [-t TOOLSETS | --toolsets TOOLSETS] [-m MODEL]")
    elif os.environ.get("FAKE_HERMES_FLAGS") == "none":
        print("usage: hermes chat [-q QUERY]")
    else:
        print("usage: hermes chat [--query-file F] [-Q] [--oneshot] [-t TOOLSETS | --toolsets TOOLSETS] "
              "[--max-turns N] [--run-budget S] [--in DIR] [-m MODEL]")
    sys.exit(0)
sys.exit(3)
'''


def _make_fake_hermes(root: Path) -> Path:
    """A venv-shaped dir whose python sees `hermes-agent` 0.21.3 metadata."""
    base = Path(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
    venv = root / "hermes-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {base.parent}\ninclude-system-site-packages = false\n")
    py = venv / "bin" / "python"
    py.symlink_to(base)
    # venv layout computed by hand: a distro sysconfig scheme may point outside the venv.
    purelib = venv / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"
    dist = purelib / "hermes_agent-0.21.3.dist-info"
    assert venv in dist.parents
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: hermes-agent\nVersion: 0.21.3\n")
    launcher = venv / "bin" / "hermes"
    launcher.write_text(f"#!{py}\n" + FAKE_HERMES)
    launcher.chmod(0o755)
    bindir = root / "usr-local-bin"
    bindir.mkdir()
    (bindir / "hermes").symlink_to(launcher)  # like /usr/local/bin/hermes -> venv launcher
    return bindir


def _make_source(root: Path, version: str = "0.2.0") -> Path:
    src = root / "hermes-wikiskill-src"
    (src / "hermes_wikiskill").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(FRAMEWORK / name, src / name)
    (src / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "hws_stub_backend"\nbackend-path = ["."]\n\n'
        f'[project]\nname = "hermes-wikiskill"\nversion = "{version}"\n')
    (src / "hws_stub_backend.py").write_text(STUB_BACKEND)
    (src / "hermes_wikiskill" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (src / "hermes_wikiskill" / "cli.py").write_text(STUB_CLI)
    return src


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        (self.home / ".hermes" / "profiles" / "editor").mkdir(parents=True)
        (self.home / ".hermes" / "config.yaml").write_text("model:\n  provider: deepseek\n")
        (self.home / ".hermes" / "profiles" / "editor" / "config.yaml").write_text("x: 1\n")
        (self.home / ".local" / "bin").mkdir(parents=True)
        user_tool = root / "user-wikiskill"
        user_tool.write_text("#!/bin/sh\necho user tool\n")
        (self.home / ".local" / "bin" / "wikiskill").symlink_to(user_tool)  # the user's own install
        self.src = _make_source(root)
        self.hermes_bin = _make_fake_hermes(root)
        self.hermes_log = root / "hermes.log"
        self.hws_log = root / "hws.log"
        self.hermes_log.touch()
        self.hws_log.touch()
        self.prefix = self.home / "ops" / "hermes-wikiskill"
        self.profile_dir = self.home / ".hermes" / "profiles" / "wikiskill"

    def run(self, script: str, *args: str, extra_env: dict | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.hermes_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HWS_PYTHON": sys.executable,
            "PIP_NO_INDEX": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "FAKE_HERMES_LOG": str(self.hermes_log),
            "STUB_HWS_LOG": str(self.hws_log),
            "HWS_MIN_DISK_MB": "1",
            "HWS_UNINSTALL_WAIT": "0",
            "LC_ALL": "C",
        }
        env.update(extra_env or {})
        env = {k: v for k, v in env.items() if v is not None}  # None drops a variable
        return subprocess.run(["bash", str(self.src / script), *args], env=env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=300, cwd=cwd)

    def hermes_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.hermes_log.read_text().splitlines() if line.strip()]

    def link(self, name: str) -> Path:
        return self.home / ".local" / "bin" / name


def _tree(root: Path, exclude: tuple[str, ...] = ()) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            if rel in exclude or p.suffix == ".pyc":
                continue
            if p.is_symlink():
                out[rel] = "->" + os.readlink(p)
            elif p.is_file():
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                out[rel] = "dir"
    return out


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _ok(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, f"exit {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return proc.stdout


# ---------------------------------------------------------------------------- static


@pytest.mark.parametrize("script", sorted([*SCRIPTS, *(p.name for p in (FRAMEWORK / "benches" / "house-report").glob("*.sh"))]))
def test_bash_syntax(script: str) -> None:
    path = FRAMEWORK / script if script in SCRIPTS else FRAMEWORK / "benches" / "house-report" / script
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_install_pip_env_is_set_before_pip_runs() -> None:
    text = (FRAMEWORK / "install.sh").read_text()
    export_at = text.index("export PIP_CACHE_DIR=$PIP_TMP PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1")
    assert export_at < text.index("-m pip install") and export_at < text.index('-m venv "$VENV"')
    assert 'rm -rf "$PIP_TMP"' in text.split("cleanup() {", 1)[1].split("}", 1)[0]


def test_scripts_never_call_hermes_version_or_profile_delete() -> None:
    for name in SCRIPTS:
        text = (FRAMEWORK / name).read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r'"\$HERMES"\s+(--version|-V)\b', code)
        assert "profile delete" not in code and "systemctl" not in code


# ---------------------------------------------------------------------------- install


def test_dry_run_writes_nothing(env: Env) -> None:
    before = _tree(env.home)
    out = _ok(env.run("install.sh", "--dry-run"))
    assert "dry-run plan" in out and "would create venv" in out and "would run: hermes-wikiskill setup-profile" in out
    assert _tree(env.home) == before
    assert not env.prefix.exists()


def test_install_twice_uninstall_reinstall(env: Env) -> None:
    out = _ok(env.run("install.sh", "--yes"))
    assert "changes: venv package symlink:hermes-wikiskill symlink:hws profile:wikiskill" in out
    assert "version 0.21.3" in out  # hermes version from package metadata
    for name in ("hermes-wikiskill", "hws"):
        assert os.readlink(env.link(name)) == str(env.prefix / "venv" / "bin" / name)
    assert (env.profile_dir / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    log = (env.prefix / "install.log").read_text()
    assert not BAD_LOG.search(log), log
    assert "doctor: OK   profile: stub" in log
    # R6: pip cache is run-local under /tmp/hws-pip-<pid>, removed at exit; never ~/.cache/pip
    pip_dirs = re.findall(r"pip cache: (/tmp/hws-pip-\d+) \(removed at exit\)", log)
    assert pip_dirs and not any(Path(d).exists() for d in pip_dirs)
    assert not (env.home / ".cache" / "pip").exists()

    calls = env.hermes_calls()
    assert calls and all(c["argv"] == ["chat", "--help"] for c in calls)
    assert all(c["HERMES_HOME"] and c["HERMES_HOME"].startswith("/tmp/hws-") for c in calls)
    assert not list(Path("/tmp").glob(Path(calls[0]["HERMES_HOME"]).name))  # probe home removed
    hws_calls = [json.loads(line) for line in env.hws_log.read_text().splitlines()]
    assert [c["argv"][0] for c in hws_calls] == ["setup-profile", "doctor"]
    assert hws_calls[0]["argv"] == ["setup-profile", "--profile", "wikiskill", "--source-profile", "default", "--json"]
    assert hws_calls[0]["HWS_HOME"] == str(env.prefix)

    # second run: nothing material changes
    before_prefix = _tree(env.prefix, exclude=("install.log",))
    before_profile = _tree(env.profile_dir)
    out2 = _ok(env.run("install.sh", "--yes"))
    assert "no changes" in out2
    assert _tree(env.prefix, exclude=("install.log",)) == before_prefix
    assert _tree(env.profile_dir) == before_profile
    assert not BAD_LOG.search((env.prefix / "install.log").read_text())

    # the user's own files are untouched throughout
    assert os.readlink(env.link("wikiskill")) == str(env.root / "user-wikiskill")
    assert (env.home / ".hermes" / "config.yaml").read_text() == "model:\n  provider: deepseek\n"

    # uninstall: backup, remove ours, keep workspaces/receipts
    (env.prefix / "workspaces" / "demo").mkdir(parents=True)
    (env.prefix / "receipts").mkdir()
    (env.prefix / "receipts" / "r1.json").write_text("{}")
    out3 = _ok(env.run("uninstall.sh", "--yes"))
    assert not env.link("hws").exists() and not env.link("hermes-wikiskill").is_symlink()
    assert not (env.prefix / "venv").exists() and not env.profile_dir.exists()
    assert not (env.prefix / "install.log").exists() and not (env.prefix / "install-state.json").exists()
    assert sorted(p.name for p in env.prefix.iterdir()) == ["receipts", "workspaces"]
    assert f"  {env.prefix}/workspaces" in out3 and f"  {env.prefix}/receipts" in out3
    backups = list((env.home / "ops" / "hermes-wikiskill-backups").iterdir())
    assert len(backups) == 1
    assert sorted(p.name for p in backups[0].iterdir()) == ["install-state.json", "install.log",
                                                             "profile-wikiskill.tgz", "receipts"]
    assert oct((backups[0] / "profile-wikiskill.tgz").stat().st_mode & 0o777) == "0o600"
    assert not (env.home / ".hermes" / "profiles" / ".deleted").exists()
    assert (env.home / ".hermes" / "profiles" / "editor" / "config.yaml").is_file()
    assert os.readlink(env.link("wikiskill")) == str(env.root / "user-wikiskill")

    # reinstall after uninstall
    out4 = _ok(env.run("install.sh", "--yes"))
    assert "changes: venv package" in out4 and env.profile_dir.is_dir()
    assert not BAD_LOG.search((env.prefix / "install.log").read_text())


def test_source_change_reinstalls_package_only(env: Env) -> None:
    _ok(env.run("install.sh", "--yes"))
    (env.src / "hermes_wikiskill" / "extra.py").write_text("X = 1\n")
    out = _ok(env.run("install.sh", "--yes"))
    assert "changes: package" in out and "venv" not in out.split("changes:")[1].split("\n")[0]
    assert (Path(next((env.prefix / "venv" / "lib").glob("python*"))) / "site-packages" / "hermes_wikiskill"
            / "extra.py").is_file()


def test_legacy_hermes_flags_warn_but_install(env: Env) -> None:
    out = _ok(env.run("install.sh", "--yes", extra_env={"FAKE_HERMES_FLAGS": "old"}))
    assert "legacy argv fallback" in out
    assert not BAD_LOG.search((env.prefix / "install.log").read_text())


def test_refusals(env: Env) -> None:
    foreign = env.link("hws")
    foreign.symlink_to("/usr/bin/true")
    proc = env.run("install.sh", "--yes")
    assert proc.returncode == 1 and "refusing to overwrite" in proc.stderr
    assert os.readlink(foreign) == "/usr/bin/true" and not env.prefix.exists()
    foreign.unlink()

    proc = env.run("install.sh", "--yes", "--profile", "default")
    assert proc.returncode == 1 and "--i-am-owner" in proc.stderr
    proc = env.run("install.sh", "--yes", extra_env={"FAKE_HERMES_FLAGS": "none"})
    assert proc.returncode == 1 and "--toolsets" in proc.stderr
    proc = env.run("install.sh", "--bogus")
    assert proc.returncode == 2
    proc = env.run("install.sh")  # no --yes and no terminal
    assert proc.returncode == 2 and "--yes" in proc.stderr
    proc = env.run("install.sh", "--yes", extra_env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 1 and "hermes not found" in proc.stderr

    proc = env.run("uninstall.sh", "--yes", "--profile", "../x")
    assert proc.returncode == 1 and "invalid profile" in proc.stderr
    assert not any(c["argv"][:1] == ["--version"] for c in env.hermes_calls())


def test_owner_flag_is_forwarded_for_default_profile(env: Env) -> None:
    out = _ok(env.run("install.sh", "--dry-run", "--profile", "default", "--i-am-owner"))
    assert "setup-profile --profile default --source-profile default --json --i-am-owner" in out
    _ok(env.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    hws_calls = [json.loads(line) for line in env.hws_log.read_text().splitlines()]
    assert hws_calls[0]["argv"] == ["setup-profile", "--profile", "default", "--source-profile", "default", "--json",
                                    "--i-am-owner"]
    # without the owner flag nothing is forwarded for a normal profile
    _ok(env.run("install.sh", "--yes"))
    later = [json.loads(line) for line in env.hws_log.read_text().splitlines()]
    assert "--i-am-owner" not in later[2]["argv"]


def test_doctor_failure_fails_install(env: Env) -> None:
    proc = env.run("install.sh", "--yes", extra_env={"STUB_DOCTOR_FAIL": "1"})
    assert proc.returncode == 1 and "doctor reported failing checks" in proc.stderr
    assert "FAIL profile" in proc.stdout


# ---------------------------------------------------------------------------- uninstall


def test_uninstall_dry_run_and_purge(env: Env) -> None:
    _ok(env.run("install.sh", "--yes"))
    (env.prefix / "workspaces" / "demo").mkdir(parents=True)
    before = _tree(env.home)
    out = _ok(env.run("uninstall.sh", "--dry-run", "--purge"))
    assert "would remove" in out and "would remove profile dir" in out
    assert _tree(env.home) == before

    out = _ok(env.run("uninstall.sh", "--yes", "--purge"))
    assert not env.prefix.exists() and not env.profile_dir.exists()
    assert "removed empty" in out
    assert len(list((env.home / "ops" / "hermes-wikiskill-backups").iterdir())) == 1


def test_uninstall_leaves_foreign_links_and_is_rerunnable(env: Env) -> None:
    foreign = env.link("hws")
    foreign.symlink_to("/usr/bin/true")
    _ok(env.run("uninstall.sh", "--yes"))
    _ok(env.run("uninstall.sh", "--yes"))
    assert os.readlink(foreign) == "/usr/bin/true"


# ---------------------------------------------------------------------------- ORCH Round 1 (ui T9)


def _live_profile(env: Env, name: str) -> Path:
    d = env.home / ".hermes" / "profiles" / name
    (d / "skills" / "clip-tools").mkdir(parents=True)
    (d / "config.yaml").write_text("model:\n  provider: deepseek\ntelegram:\n  enabled: true\n")
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=fake-bot\n")
    (d / "skills" / "clip-tools" / "SKILL.md").write_text("x\n")
    return d


def _set_state_profile(env: Env, profile: str) -> None:
    p = env.prefix / "install-state.json"
    d = json.loads(p.read_text())
    d["profile"] = profile
    p.write_text(json.dumps(d, sort_keys=True) + "\n")


def test_r1_04_uninstall_never_deletes_a_foreign_profile(env: Env) -> None:
    _ok(env.run("install.sh", "--yes"))
    clip = _live_profile(env, "otherprofile")
    before = _tree(clip)
    proc = env.run("uninstall.sh", "--yes", "--profile", "otherprofile")
    assert proc.returncode == 1 and "never touched" in proc.stderr
    assert _tree(clip) == before
    assert (env.prefix / "venv").is_dir() and env.profile_dir.is_dir()  # refused before removing anything


def test_r1_04_foreign_profile_in_state_is_kept_and_operator_skill_undone(env: Env) -> None:
    _ok(env.run("install.sh", "--yes"))
    clip = _live_profile(env, "otherprofile")
    _set_state_profile(env, "otherprofile")
    (env.prefix / "receipts").mkdir()  # R2-02: undo is driven by its un-removed operator-skill receipt
    (env.prefix / "receipts" / "20260929T000000Z-setup-otherprofile.json").write_text(json.dumps(
        {"kind": "operator-skill", "id": "20260929T000000Z-setup-otherprofile", "profile": "otherprofile"}))
    before = _tree(clip)
    out = _ok(env.run("uninstall.sh", "--yes"))
    assert "profile: otherprofile (foreign)" in out and "kept (not created by hermes-wikiskill)" in out
    assert _tree(clip) == before
    calls = [json.loads(line) for line in env.hws_log.read_text().splitlines()]
    assert calls[-1]["argv"] == ["setup-profile", "--profile", "otherprofile", "--remove", "--i-am-owner", "--json"]
    assert calls[-1]["HWS_HOME"] == str(env.prefix)
    assert not (env.prefix / "venv").exists() and not env.link("hws").is_symlink()


def test_r1_07_uninstall_undoes_default_operator_skill_and_stops_on_mismatch(env: Env) -> None:
    _ok(env.run("install.sh", "--yes", "--profile", "default", "--i-am-owner"))
    cfg = (env.home / ".hermes" / "config.yaml").read_bytes()
    proc = env.run("uninstall.sh", "--yes", extra_env={"STUB_REMOVE_RC": "4"})
    assert proc.returncode == 1 and "changed since it was installed" in proc.stderr
    assert (env.prefix / "venv").is_dir()  # nothing removed
    out = _ok(env.run("uninstall.sh", "--yes"))
    assert "profile: default (foreign)" in out
    calls = [json.loads(line) for line in env.hws_log.read_text().splitlines()]
    assert calls[-1]["argv"] == ["setup-profile", "--profile", "default", "--remove", "--i-am-owner", "--json"]
    assert (env.home / ".hermes").is_dir() and (env.home / ".hermes" / "config.yaml").read_bytes() == cfg


def _fake_current_install(env: Env) -> Path:
    cur = env.home / "ops" / "wikiskill"
    (cur / "venv" / "bin").mkdir(parents=True)
    (cur / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (cur / "workspaces" / "demo").mkdir(parents=True)
    (cur / "workspaces" / "demo" / "tasks.json").write_text("[]\n")
    return cur


@pytest.mark.parametrize("prefix", ["ops/wikiskill", "ops/wikiskill/", "ops/wikiskill/sub", "ops", ".hermes",
                                    ".hermes/hermes-agent", ".", ".."])
def test_r1_05_protected_prefixes_refused_by_both_scripts(env: Env, prefix: str) -> None:
    cur = _fake_current_install(env)
    before, hermes_before = _tree(cur), _tree(env.home / ".hermes")
    for script, extra in (("install.sh", ()), ("uninstall.sh", ("--purge",))):
        proc = env.run(script, "--yes", *extra, "--prefix", prefix, cwd=env.home)
        assert proc.returncode == 2 and "refusing prefix" in proc.stderr, (script, proc.stderr)
    assert _tree(cur) == before and _tree(env.home / ".hermes") == hermes_before


def test_r1_05_foreign_venv_without_install_state_is_refused(env: Env) -> None:
    other = env.home / "opt" / "tool"
    (other / "venv" / "bin").mkdir(parents=True)
    (other / "workspaces" / "w").mkdir(parents=True)
    before = _tree(other)
    proc = env.run("install.sh", "--yes", "--prefix", str(other))
    assert proc.returncode == 1 and "not created by this installer" in proc.stderr
    proc = env.run("uninstall.sh", "--yes", "--purge", "--prefix", str(other))
    assert proc.returncode == 1 and "install-state.json is missing" in proc.stderr
    assert _tree(other) == before


def test_r1_22_prefix_canonicalised_and_purge_backs_up_workspaces(env: Env) -> None:
    _ok(env.run("install.sh", "--yes", "--prefix", "ops/./hermes-wikiskill//", cwd=env.home))
    assert os.readlink(env.link("hws")) == str(env.prefix / "venv" / "bin" / "hws")  # no // or /./
    (env.prefix / "workspaces" / "demo").mkdir(parents=True)
    (env.prefix / "workspaces" / "demo" / "tasks.json").write_text("[1]\n")
    out = _ok(env.run("uninstall.sh", "--yes", "--purge", "--prefix", str(env.prefix) + "/"))
    assert not env.link("hws").is_symlink() and not env.link("hermes-wikiskill").is_symlink()
    assert not env.prefix.exists() and not env.profile_dir.exists()
    [backup] = list((env.home / "ops" / "hermes-wikiskill-backups").iterdir())
    assert f"workspaces backed up: {backup}/workspaces.tgz" in out
    import tarfile
    with tarfile.open(backup / "workspaces.tgz") as tar:
        assert "workspaces/demo/tasks.json" in tar.getnames()


def test_r1_24_pip_cache_is_run_local_and_removed(env: Env) -> None:
    build_log = env.root / "build.log"
    out = _ok(env.run("install.sh", "--yes", extra_env={"PIP_NO_CACHE_DIR": None, "STUB_BUILD_LOG": str(build_log)}))
    [pip_dir] = re.findall(r"pip cache: (/tmp/hws-pip-\d+) \(removed at exit\)", out)
    assert build_log.read_text().splitlines() == [f"PIP_CACHE_DIR={pip_dir}"]  # pip ran with OUR cache dir
    assert not Path(pip_dir).exists() and not (env.home / ".cache").exists()


def test_r1_06_install_help_says_what_owner_flag_does() -> None:
    text = (FRAMEWORK / "install.sh").read_text()
    usage = text.split("usage() {", 1)[1].split("EOF", 2)[1]
    assert "ONLY the operator skill" in usage and "config.yaml/.env/auth.json are never" in usage
    assert "--remove --i-am-owner" in usage


# ---------------------------------------------------------------------------- effectiveness scripts

BENCH = FRAMEWORK / "benches" / "house-report"

STUB_HWS = r'''
import json, os, shutil, sys

args = sys.argv[1:]
with open(os.environ["STUB_HWS_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": args, "lock": os.environ.get("HWS_ROLLOUT_LOCK")}) + "\n")
assert args[0] == "--ws-root"
root, verb, ws = args[1], args[2], args[3]
opts = {args[i]: args[i + 1] for i in range(4, len(args) - 1) if args[i].startswith("--")}
wsd = os.path.join(root, ws)
state_path = os.path.join(wsd, "runs", "state.json")


def save(state):
    with open(state_path, "w") as fh:
        json.dump(state, fh)


if verb == "init":
    os.makedirs(os.path.join(wsd, "runs"))
    os.makedirs(os.path.join(wsd, "skills", "active"))
    shutil.copy(opts["--tasks"], os.path.join(wsd, "tasks.json"))
elif verb == "evolve":
    k = int(opts["--iters"])
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"next_iter": 1, "history": []}
    if state.get("baseline") is None:
        state["baseline"] = state["r_best"] = float(os.environ.get("STUB_BASELINE", "0.25"))
    rvals = [float(x) for x in os.environ.get("STUB_RVALS", "0.5,0.85,0.9,0.95").split(",")]
    for it in range(state["next_iter"], k + 1):
        r_val = rvals[it - 1]
        accepted = r_val > state["r_best"]
        if accepted:
            state["r_best"] = r_val
            d = os.path.join(wsd, "skills", "active", "house-style")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "SKILL.md"), "w").write("x")
        state["history"].append({"iter": it, "r_val": r_val, "accepted": accepted, "proposal": "p"})
        state["next_iter"] = it + 1
    save(state)
elif verb == "eval":
    ids = [t["id"] for t in json.load(open(opts["--tasks"]))]
    arm, tag = opts["--skills"], opts["--tag"]
    reps = []
    for rep in range(1, int(opts["--repeat"]) + 1):
        results = []
        for i, tid in enumerate(ids):
            hit = arm == "active" and i % (rep + 1) == 0
            flagged = (arm, rep, i) == ("empty", 1, 0)  # STUB_FLAG: one escape-forced zero + one exit 1
            results.append({"task_id": tid, "score": 1.0 if hit else 0.0, "usage": {"cost_usd": 0.001},
                            "skill_views": ["house-style"] if hit else [], "session": f"s-{arm}-{rep}-{i}",
                            "exit_code": 1 if flagged and os.environ.get("STUB_FLAG") else 0,
                            "sandbox_escape": ["tasks.json"] if flagged and os.environ.get("STUB_FLAG") else []})
        reps.append({"rep": rep, "mean": sum(r["score"] for r in results) / len(results), "results": results})
    import hashlib
    summary = {"tag": tag, "skills": arm, "skill_names": ["house-style"] if arm == "active" else [],
               "model": os.environ.get("STUB_MODEL_" + arm, "m"), "max_turns": int(opts["--max-turns"]),
               "hermes_version": "0.21.3", "tasks_file": opts["--tasks"], "active_commit": "c0ffee",
               "tasks_sha256": hashlib.sha256(open(opts["--tasks"], "rb").read()).hexdigest(),
               "repeats": reps, "mean": sum(r["mean"] for r in reps) / len(reps)}
    os.makedirs(os.path.join(wsd, "eval", tag))
    json.dump(summary, open(os.path.join(wsd, "eval", tag, "summary.json"), "w"))
else:
    sys.exit(2)
'''


class Eff:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ws_root = root / "workspaces"
        self.ws_root.mkdir()
        self.hws = root / "hws"
        self.hws.write_text(f"#!{sys.executable}\n" + STUB_HWS)
        self.hws.chmod(0o755)
        self.log = root / "hws.log"
        self.log.touch()

    def run(self, script: str, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.root), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HWS_CLI": str(self.hws),
               "HWS_PY": sys.executable, "STUB_HWS_LOG": str(self.log), "HWS_WS_ROOT": str(self.ws_root),
               "LC_ALL": "C.UTF-8"}
        env.update(extra_env or {})
        return subprocess.run(["bash", str(BENCH / script), *args], env=env, capture_output=True, text=True,
                              timeout=120)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


@pytest.fixture()
def eff(tmp_path: Path) -> Eff:
    return Eff(tmp_path)


def test_run_effectiveness_happy_path(eff: Eff) -> None:
    out = _ok(eff.run("run-effectiveness.sh", "eff1"))
    verbs = [(c["argv"][2], c["argv"][c["argv"].index("--iters") + 1] if "--iters" in c["argv"] else
              c["argv"][c["argv"].index("--skills") + 1] if "--skills" in c["argv"] else "") for c in eff.calls()]
    # baseline, iter 1 (0.5 accept), iter 2 (0.85 accept >= 0.8) -> early stop, then empty + active evals
    assert verbs == [("init", ""), ("evolve", "0"), ("evolve", "1"), ("evolve", "2"), ("eval", "empty"),
                     ("eval", "active")]
    for c in eff.calls():
        assert c["lock"] and c["lock"].endswith("ops/.hws-team.lock")
        if c["argv"][2] in ("init", "evolve"):
            assert not any(a.endswith("test.json") for a in c["argv"])
        else:
            assert c["argv"][c["argv"].index("--tasks") + 1] == str(BENCH / "test.json")
    ws = eff.ws_root / "eff1"
    assert not (ws / "test.json").exists()
    freeze = json.loads((ws / "eval" / "freeze.json").read_text())
    assert freeze["test_sha256"] == hashlib.sha256((BENCH / "test.json").read_bytes()).hexdigest()
    assert freeze["active_skills"] == "house-style"

    result = json.loads((ws / "eval" / "effectiveness.json").read_text())
    n = len(json.loads((BENCH / "test.json").read_text()))
    rep1, rep2 = len(range(0, n, 2)) / n, len(range(0, n, 3)) / n
    assert result["empty_means"] == [0.0, 0.0]
    assert result["active_means"] == [rep1, rep2]
    assert result["delta"] == round((rep1 + rep2) / 2, 6)
    assert result["skill_view_receipts"]["count"] == len(range(0, n, 2)) + len(range(0, n, 3))
    assert result["skill_view_receipts"]["empty_arm_views_of_evolved"] == 0
    assert result["cost"]["active"] == {"total_usd": round(0.001 * 2 * n, 6), "runs": 2 * n, "runs_without_cost": 0}
    assert result["threshold"]["met"] is True and result["problems"] == []
    assert len(result["per_task"]) == n
    assert "| empty | 0 | 0 | 0 |" in out and "**Δ (evolved − empty) = +" in out
    # QA-F8: the held-out file is named by basename + sha256 only; no path outside the ws in what we write there
    assert result["tasks_file"] == "test.json"
    assert result["tasks_sha256"] == freeze["test_sha256"]
    assert result["summaries"] == {"empty": "eval/test-empty/summary.json", "active": "eval/test-active/summary.json"}
    for written in ("effectiveness.json", "freeze.json"):
        assert str(BENCH) not in (ws / "eval" / written).read_text()

    # QA recompute from the same summaries, no new runs
    before = len(eff.calls())
    again = _ok(eff.run("measure.sh", "eff1", "--reuse"))
    assert len(eff.calls()) == before and "threshold met: True" in again
    assert json.loads((ws / "eval" / "effectiveness.json").read_text()) == result
    # re-running without --reuse must not overwrite existing tags
    proc = eff.run("measure.sh", "eff1")
    assert proc.returncode == 1 and "exists" in proc.stderr
    # a new prefix measures again under new tags
    _ok(eff.run("measure.sh", "eff1", "--tag-prefix", "qa-"))
    assert (ws / "eval" / "qa-effectiveness.json").is_file() and (ws / "eval" / "qa-test-active").is_dir()


def test_run_effectiveness_aborts_when_bench_too_easy(eff: Eff) -> None:
    proc = eff.run("run-effectiveness.sh", "easy", extra_env={"STUB_BASELINE": "0.5"})
    assert proc.returncode == 3 and "BEFORE any test eval" in proc.stdout
    assert [c["argv"][2] for c in eff.calls()] == ["init", "evolve"]


def test_run_effectiveness_no_accept_does_not_measure(eff: Eff) -> None:
    proc = eff.run("run-effectiveness.sh", "flat", "--max-iters", "2", extra_env={"STUB_RVALS": "0.1,0.2"})
    assert proc.returncode == 4
    assert "eval" not in [c["argv"][2] for c in eff.calls()]


def test_measure_flags_mismatched_arms(eff: Eff) -> None:
    _ok(eff.run("run-effectiveness.sh", "mm", "--max-iters", "1", "--measure-without-accept",
                extra_env={"STUB_RVALS": "0.1"}))
    proc = eff.run("measure.sh", "mm", "--tag-prefix", "x-", extra_env={"STUB_MODEL_active": "other-model"})
    assert proc.returncode == 1 and "arms differ in model" in proc.stdout
    result = json.loads((eff.ws_root / "mm" / "eval" / "x-effectiveness.json").read_text())
    assert result["threshold"]["met"] is False


def test_r1_27_measure_flags_escapes_and_nonzero_exits_without_failing(eff: Eff) -> None:
    _ok(eff.run("run-effectiveness.sh", "fl", "--max-iters", "1", "--no-measure", extra_env={"STUB_RVALS": "0.9"}))
    out = _ok(eff.run("measure.sh", "fl", extra_env={"STUB_FLAG": "1"}))  # graded outcomes: not problems, exit 0
    result = json.loads((eff.ws_root / "fl" / "eval" / "effectiveness.json").read_text())
    ids = [t["id"] for t in json.loads((BENCH / "test.json").read_text())]
    first, n = ids[0], len(ids)
    assert result["flags"]["empty"] == {"escapes": 1, "escaped": [f"rep1:{first}"],
                                        "nonzero_exits": [f"rep1:{first}(exit 1)"]}
    assert result["flags"]["active"] == {"escapes": 0, "escaped": [], "nonzero_exits": []}
    assert result["problems"] == []
    assert any("empty arm: 1 sandbox escape" in x for x in result["notes"])
    assert any("exited non-zero with a graded session" in x for x in result["notes"])
    # delta is never re-scored; delta_excluding_flagged drops the escaped task from BOTH arms (all repeats)
    rep1, rep2 = len(range(0, n, 2)) / n, len(range(0, n, 3)) / n
    assert result["delta"] == round((rep1 + rep2) / 2, 6)
    kept_hits = len(range(2, n, 2)) + len(range(3, n, 3))  # stub hits minus task index 0, over 2 x (n - 1) runs
    assert result["excluded_tasks"] == [first]
    assert result["delta_excluding_flagged"] == round(kept_hits / (2 * (n - 1)), 6)
    assert result["threshold"]["met"] is True and "Δ excluding escape-flagged tasks" in out


def test_r1_27_met_needs_delta_excluding_flagged(eff: Eff, tmp_path: Path) -> None:
    """Escape-forced zeros in the EMPTY arm could fake a gain: met also needs the flagged-free delta."""
    ws = eff.ws_root / "esc"
    for arm, scores in (("empty", [0.0, 1.0]), ("active", [1.0, 1.0])):
        reps = [{"rep": r, "mean": sum(scores) / 2,
                 "results": [{"task_id": f"t{i}", "score": sc, "exit_code": 0,
                              "sandbox_escape": ["tasks.json"] if arm == "empty" and i == 0 else []}
                             for i, sc in enumerate(scores)]} for r in (1, 2)]
        d = ws / "eval" / f"test-{arm}"
        d.mkdir(parents=True)
        (d / "summary.json").write_text(json.dumps({"skills": arm, "model": "m", "max_turns": 15, "repeats": reps,
                                                    "hermes_version": "0.21.3", "skill_names": []}))
    tasks = tmp_path / "t.json"
    tasks.write_text("[]")
    proc = eff.run("measure.sh", "esc", "--reuse", "--tasks", str(tasks))
    assert proc.returncode == 0, proc.stderr
    result = json.loads((ws / "eval" / "effectiveness.json").read_text())
    assert result["delta"] == 0.5 and result["excluded_tasks"] == ["t0"] and result["delta_excluding_flagged"] == 0.0
    assert result["threshold"]["met"] is False and result["problems"] == []


def test_effectiveness_scripts_usage_errors(eff: Eff) -> None:
    assert eff.run("run-effectiveness.sh").returncode == 2
    assert eff.run("run-effectiveness.sh", "w", "--max-iters", "5").returncode == 2
    assert eff.run("measure.sh", "nows").returncode == 1
    assert eff.run("stock-compare.sh", "--iters", "3").returncode == 2
