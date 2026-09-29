"""CLI surface, packaging (D4: fixes live in this distribution's source), framework skills."""

import importlib.metadata
import json
import os

import pytest

from conftest import fw_ws, hws, impls, import_upstream, mini_tasks
from hermes_wikiskill import __version__, resources
from hermes_wikiskill.backends import hermes


def test_cli_version_json():
    r = hws("version", "--json")
    assert r.returncode == 0
    d = json.loads(r.stdout)
    assert d["tool"] == "hermes-wikiskill" and d["version"] == __version__ == "0.2.0"
    assert set(d) == {"tool", "version", "hermes_version", "python"}


def test_cli_help_lists_upstream_and_new_verbs():
    out = hws("--help").stdout
    for verb in ("init", "bench", "status", "evolve", "gate", "compare", "transfer", "run-task",
                 "maintain", "propose", "reset", "eval", "report", "version"):
        assert verb in out


def test_cli_usage_error_exit_2():
    assert hws("evolve").returncode == 2


@impls("D4: stock wikiskill 0.1.5 ships DEFAULT_TOOLSETS without skills (fix only as hand patch)")
def test_d04_fix_is_in_distribution_source(impl):
    if impl == "framework":
        dist = importlib.metadata.distribution("hermes-wikiskill")
        toolsets = hermes.DEFAULT_TOOLSETS
        assert dist.metadata["Name"] == "hermes-wikiskill"
    else:
        import_upstream()
        from wikiskill.backends import hermes as uh
        toolsets = uh.DEFAULT_TOOLSETS
    assert "skills" in toolsets.split(",")


def test_d04_framework_does_not_import_upstream_package():
    import hermes_wikiskill
    pkg = os.path.dirname(hermes_wikiskill.__file__)
    for root, _, files in os.walk(pkg):
        for f in files:
            if f.endswith(".py"):
                src = open(os.path.join(root, f), encoding="utf-8").read()
                assert "import wikiskill" not in src and "from wikiskill" not in src, f


def test_framework_skills_packaged_and_verified(tmp_path):
    found = dict(resources.require_framework_skills())
    assert set(found) == {"wikiskill-maintainer", "wikiskill-proposer"}
    with pytest.raises(FileNotFoundError):
        resources.require_framework_skills(str(tmp_path))


def test_broken_staged_framework_skill_is_repaired(tmp_path):
    from hermes_wikiskill import harness
    ws = fw_ws(tmp_path)
    p = os.path.join(ws, "skills", "framework", "wikiskill-maintainer", "SKILL.md")
    os.remove(p)
    harness.ensure_framework_skills(ws)
    assert os.path.isfile(p)


def test_init_default_bench_is_house_report(tmp_path):
    if not resources.bench_dir():
        pytest.skip("bench not present in this checkout")
    r = hws("init", str(tmp_path / "ws"), "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["tasks_file"].endswith(os.path.join("house-report", "tasks.json"))
    assert d["train"] > 0 and d["val"] > 0
    assert hws("init", str(tmp_path / "ws")).returncode == 1  # already exists


def test_report_verb_uses_workspace_artifacts(tmp_path):
    ws = fw_ws(tmp_path, mini_tasks())
    out = tmp_path / "dash.html"
    r = hws("report", ws, "--json", "--html", str(out), "--acceptance-dir", str(tmp_path / "none"))
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["ws"] == ws and out.read_text().lstrip().lower().startswith("<!doctype html")


def test_reset_clears_runs_and_rolls_back(tmp_path, answers):
    from conftest import add_skill
    ts = mini_tasks()
    answers(ts)
    ws = fw_ws(tmp_path, ts)
    assert hws("run-task", ws, "tr-0").returncode == 0
    add_skill(ws, "uncommitted", commit=False)
    assert hws("reset", ws).returncode == 0
    assert not os.path.exists(os.path.join(ws, "raw"))
    assert not os.path.exists(os.path.join(ws, "skills", "active", "uncommitted"))


def test_transfer_between_workspaces(tmp_path):
    from conftest import add_skill
    a, b = fw_ws(tmp_path, name="a"), fw_ws(tmp_path, name="b")
    add_skill(a)
    r = hws("transfer", a, b, "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["transferred"] == ["house-report"]
    assert os.path.isfile(os.path.join(b, "skills", "active", "house-report", "SKILL.md"))


def test_d04_wheel_build_hook_ships_operator_skill_and_benches(tmp_path, monkeypatch):
    """setup.py's build_py hook copies framework/skills + benches into the wheel's package data."""
    import runpy
    import setuptools
    from setuptools.command import build_py as bp
    monkeypatch.setattr(setuptools, "setup", lambda **kw: kw)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mod = runpy.run_path(os.path.join(here, "setup.py"))
    monkeypatch.setattr(bp.build_py, "run", lambda self: None)  # skip the normal module copy
    cmd = mod["BuildWithData"].__new__(mod["BuildWithData"])
    cmd.build_lib = str(tmp_path)
    cmd.run()
    data = tmp_path / "hermes_wikiskill" / "data"
    assert (data / "skills" / "hermes-wikiskill" / "SKILL.md").is_file()
    assert (data / "benches" / "house-report" / "tasks.json").is_file()
    assert (data / "benches" / "house-report" / "test.json").is_file()
    assert resources.operator_skill_dir() is not None
