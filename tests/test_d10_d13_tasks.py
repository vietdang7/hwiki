"""D10 sandbox containment, D12 code_stdout exit code, D13 bench replacement re-materializes."""

import os

import pytest

from conftest import fw_ws, impls, import_upstream, make_task, mini_tasks, up_ws
from hermes_wikiskill import scoring, tasks as tasks_mod
from hermes_wikiskill.errors import UnsafePathError


@impls("D10: upstream materialize writes sandbox paths like ../x outside bench/tasks/<id>")
@pytest.mark.parametrize("rel", ["../escape.txt", "../../escape.txt"])
def test_d10_sandbox_traversal_rejected(impl, rel, tmp_path):
    t = make_task("bad", "train")
    t["sandbox"] = {rel: "pwned"}
    ws = tmp_path / "ws"
    (ws / "bench" / "tasks").mkdir(parents=True)
    if impl == "framework":
        with pytest.raises(UnsafePathError):
            tasks_mod.materialize(str(ws), t)
    else:
        import_upstream()
        from wikiskill import tasks as ut
        try:
            ut.materialize(str(ws), t)
        except Exception:
            pass
    assert not any(p.name == "escape.txt" for p in tmp_path.rglob("escape.txt"))


def test_d10_absolute_sandbox_path_rejected_by_validate():
    t = make_task("bad", "train")
    t["sandbox"] = {"/tmp/hws-escape": "x"}
    with pytest.raises(UnsafePathError):
        tasks_mod.validate([t])


def test_d10_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    d = tmp_path / "sb"
    d.mkdir()
    (d / "link").symlink_to(outside)
    t = make_task("x", "train")
    t["sandbox"] = {"link/f.txt": "x"}
    with pytest.raises(UnsafePathError):
        tasks_mod.materialize_at(str(d), t)
    assert not (outside / "f.txt").exists()


@impls("D12: upstream code_stdout ignores the script's exit code")
def test_d12_code_stdout_requires_exit_zero(impl, tmp_path):
    (tmp_path / "solve.py").write_text("print('42')\nraise SystemExit(1)\n")
    task = {"grader": {"type": "code_stdout", "script": "solve.py", "expected": "42"}}
    if impl == "framework":
        score = scoring.grade(task, str(tmp_path))
    else:
        import_upstream()
        from wikiskill import scoring as us
        score = us.grade(task, str(tmp_path))
    assert score == 0.0


def test_d12_code_stdout_passes_on_exit_zero(tmp_path):
    (tmp_path / "solve.py").write_text("print('42')\n")
    assert scoring.grade({"grader": {"type": "code_stdout", "script": "solve.py",
                                     "expected": "42"}}, str(tmp_path)) == 1.0


@impls("D13: upstream `bench` keeps old sandbox files when the task set changes (no --reset)")
def test_d13_replacing_tasks_rematerializes_sandboxes(impl, tmp_path):
    old = mini_tasks()
    new = [dict(t, sandbox={"data.csv": "NEW DATA\n"}) for t in old]
    if impl == "framework":
        ws = fw_ws(tmp_path, old)
        tasks_mod.replace_tasks(ws, new)
    else:
        import_upstream()
        from wikiskill import tasks as ut
        ws = up_ws(tmp_path, old)
        ut.save(ws, new)  # what cli.cmd_bench does without --reset
        ut.materialize_all(ws, new, force=False)
    for t in new:
        with open(os.path.join(ws, "bench", "tasks", t["id"], "data.csv")) as f:
            assert f.read() == "NEW DATA\n"


def test_d13_removed_tasks_lose_their_sandboxes(tmp_path):
    ws = fw_ws(tmp_path)
    out = tasks_mod.replace_tasks(ws, mini_tasks(1, 1))
    assert out["removed_sandboxes"] == ["tr-1", "va-1"]
    assert sorted(os.listdir(os.path.join(ws, "bench", "tasks"))) == ["tr-0", "va-0"]
