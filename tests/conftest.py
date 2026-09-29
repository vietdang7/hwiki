"""Shared fixtures: an isolated fake Hermes, a fake credential source profile, tiny
train/val benches, and helpers to build framework and stock-upstream workspaces.

Upstream = the PyPI ``wikiskill==0.1.5`` package (test extra). Tests parametrized over
``impl`` run the same scenario against both; the upstream param is a strict xfail that
documents the defect (it must FAIL there)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_BIN = os.path.join(HERE, "fake_hermes")
FAKE_HERMES = os.path.join(FAKE_BIN, "hermes")

# fixture secrets must never match the C7 secret-scan regexes
SOURCE_ENV = "\n".join([
    "DEEPSEEK_API_KEY=fake-key-1",
    "DEEPSEEK_BASE_URL=https://api.deepseek.test/v1",
    "TELEGRAM_BOT_TOKEN=fake-bot",
    "GROQ_API_KEY=fake-groq",
]) + "\n"
SOURCE_CONFIG = """model:
  provider: deepseek
  default: deepseek-v4-flash
  base_url: https://api.deepseek.test/v1
  api_key: fake-inline
providers:
  deepseek:
    api_key: fake-inline
agent:
  disabled_toolsets:
    - skills
telegram:
  bot_token: fake-bot
"""


def upstream_param(reason: str):
    return pytest.param("upstream", marks=pytest.mark.xfail(strict=True, reason=reason))


def impls(reason: str):
    return pytest.mark.parametrize("impl", ["framework", upstream_param(reason)])


def import_upstream():
    return pytest.importorskip("wikiskill")


@pytest.fixture(autouse=True)
def fake_env(tmp_path, monkeypatch):
    """Every test: fake hermes first on PATH, private HWS_HOME/source profile, no ambient locks."""
    from hermes_wikiskill import compat

    compat._CACHE.clear()
    src = tmp_path / "source-home"
    src.mkdir()
    (src / ".env").write_text(SOURCE_ENV)
    (src / "config.yaml").write_text(SOURCE_CONFIG)
    (src / "auth.json").write_text(json.dumps({"providers": {"nous": {"token": "fake-nous"}}}))
    monkeypatch.setenv("PATH", FAKE_BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HERMES_BIN", FAKE_HERMES)
    monkeypatch.setenv("HWS_SOURCE_HOME", str(src))
    monkeypatch.setenv("HERMES_HOME", str(src))  # what the stock upstream copies from
    monkeypatch.setenv("HWS_HOME", str(tmp_path / "hws"))
    monkeypatch.setenv("HWS_HERMES_ROOT", str(tmp_path / "hermes-root"))  # never the live ~/.hermes
    monkeypatch.setenv("HWS_ROLLOUT_ROOT", str(tmp_path / "rollouts"))  # outside ws and HWS_HOME
    monkeypatch.setenv("FAKE_HERMES_LOG", str(tmp_path / "fake-hermes.log"))
    # QA-F3: hermes children get an allowlisted env; the test doubles' knobs pass explicitly
    # R1-19: HWS_PASS_ENV is honoured only in test mode
    monkeypatch.setenv("HWS_TESTING", "1")
    monkeypatch.setenv("HWS_PASS_ENV", "FAKE_HERMES_*,QA_HERMES_LOG,QA_DROP_SKILL")
    for k in ("HWS_ROLLOUT_LOCK", "HWS_SOURCE_PROFILE", "FAKE_HERMES_FLAGS", "FAKE_HERMES_EXIT",
              "FAKE_HERMES_HIDE_SKILLS", "FAKE_HERMES_PROPOSAL", "FAKE_HERMES_ALWAYS_SOLVE"):
        monkeypatch.delenv(k, raising=False)
    return {"source": src, "tmp": tmp_path}


def fake_log(tmp_path) -> list[dict]:
    p = tmp_path / "fake-hermes.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def make_task(tid: str, split: str, answer: str = "== OK ==\n") -> dict:
    return {"id": tid, "split": split, "title": f"report {tid}",
            "prompt": f"Create report.txt for report id {tid} in the house format.",
            "sandbox": {"data.csv": f"id,amount\n{tid},1\n"},
            "grader": {"type": "exact", "file": "report.txt", "expected": answer}}


def mini_tasks(n_train: int = 2, n_val: int = 2) -> list[dict]:
    return ([make_task(f"tr-{i}", "train") for i in range(n_train)] +
            [make_task(f"va-{i}", "val") for i in range(n_val)])


@pytest.fixture
def answers(tmp_path, monkeypatch):
    """Answers the fake agent writes when a FAKE-SOLVE skill is visible."""
    def install(tasks):
        p = tmp_path / "answers.json"
        p.write_text(json.dumps({t["id"]: {t["grader"]["file"]: t["grader"]["expected"]}
                                 for t in tasks}))
        monkeypatch.setenv("FAKE_HERMES_ANSWERS", str(p))
        return p
    return install


def fw_ws(tmp_path, tasks=None, name="ws") -> str:
    from hermes_wikiskill import harness

    ws = str(tmp_path / name)
    harness.init_workspace(ws, tasks or mini_tasks())
    return ws


def up_ws(tmp_path, tasks=None, name="upws") -> str:
    up = import_upstream()
    from wikiskill import harness as uh, tasks as ut

    ws = str(tmp_path / name)
    uh.init_workspace(ws)
    ut.save(ws, tasks or mini_tasks())
    ut.materialize_all(ws, tasks or mini_tasks())
    assert up
    return ws


def add_skill(ws: str, name: str = "house-report", body: str = "FAKE-SOLVE rules", commit=True):
    d = os.path.join(ws, "skills", "active", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w") as f:
        f.write(f"---\nname: {name}\ndescription: ACME house report format\n---\n{body}\n")
    if commit:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "add", "-A"], cwd=os.path.dirname(d), check=True, env=env)
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-qm", f"add {name}"],
                       cwd=os.path.dirname(d), check=True, env=env)
    return d


def tree_hash(root: str) -> str:
    """Hash of every path, file content, symlink target and git ref under root."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        h.update(f"D {rel}\n".encode())
        for n in sorted(filenames):
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                h.update(f"L {rel}/{n} {os.readlink(p)}\n".encode())
            else:
                with open(p, "rb") as f:
                    h.update(f"F {rel}/{n} ".encode() + hashlib.sha256(f.read()).digest())
        for n in sorted(dirnames):
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                h.update(f"L {rel}/{n} {os.readlink(p)}\n".encode())
    return h.hexdigest()


def hws(*args, env=None, cwd=None):
    """Run the CLI in-process-free (subprocess) and return CompletedProcess."""
    import sys
    return subprocess.run([sys.executable, "-m", "hermes_wikiskill", *args], capture_output=True,
                          text=True, env={**os.environ, **(env or {})}, cwd=cwd)
