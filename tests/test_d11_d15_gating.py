"""D11 proposal containment; D15 checked git + exact rollback."""

import os
import subprocess

import pytest

from conftest import fw_ws, impls, import_upstream, up_ws
from hermes_wikiskill import gating, gitutil
from hermes_wikiskill.errors import GitError, UnsafePathError

EVIL = [
    {"action": "create", "name": "../../escaped", "skill_md": "x"},
    {"action": "create", "name": "/tmp/hws-abs-escape", "skill_md": "x"},
    {"action": "patch", "name": "ok", "file": "../../../canary.md",
     "edits": [{"op": "append", "content": "x"}]},
]


@impls("D11: upstream joins LLM-written proposal names/paths without containment")
@pytest.mark.parametrize("proposal", EVIL[:1] + EVIL[2:], ids=["create-dotdot", "patch-dotdot"])
def test_d11_proposal_cannot_write_outside_active(impl, proposal, tmp_path):
    if impl == "framework":
        ws = fw_ws(tmp_path)
        apply = gating.apply_proposal
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path)
        apply = ug.apply_proposal
    ok = os.path.join(ws, "skills", "active", "ok")
    os.makedirs(ok)
    open(os.path.join(ok, "SKILL.md"), "w").write("---\nname: ok\n---\n")
    canary = os.path.join(ws, "canary.md")
    open(canary, "w").write("original")
    before = {p for p in tmp_path.rglob("*") if ".git" not in p.parts}
    try:
        apply(ws, proposal)
    except Exception:
        pass
    after = {p for p in tmp_path.rglob("*") if ".git" not in p.parts}
    assert not [p for p in after - before if "skills/active" not in str(p)]
    assert open(canary).read() == "original"


def test_d11_bad_names_and_symlinks_refused(tmp_path):
    ws = fw_ws(tmp_path)
    with pytest.raises(UnsafePathError):
        gating.apply_proposal(ws, EVIL[1])
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, os.path.join(ws, "skills", "active", "linked"))
    with pytest.raises(UnsafePathError):
        gating.apply_proposal(ws, {"action": "create", "name": "linked", "skill_md": "x"})
    assert not list(outside.iterdir())


def test_d11_created_skill_gets_frontmatter_named_after_dir(tmp_path):
    ws = fw_ws(tmp_path)
    gating.apply_proposal(ws, {"action": "create", "name": "house-report",
                               "skill_md": "---\nname: Something Else\ndescription: d\n---\nbody"})
    text = open(os.path.join(ws, "skills", "active", "house-report", "SKILL.md")).read()
    assert text.startswith("---\nname: house-report\n")
    gating.apply_proposal(ws, {"action": "create", "name": "plain", "skill_md": "# Title\nbody"})
    text = open(os.path.join(ws, "skills", "active", "plain", "SKILL.md")).read()
    assert text.startswith("---\nname: plain\ndescription: \"Title\"\n---")


def _failing_git(tmp_path, monkeypatch, verb):
    bindir = tmp_path / "gitshim"
    bindir.mkdir()
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = bindir / "git"
    shim.write_text(f"#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = \"{verb}\" ] && exit 1; done\nexec {real} \"$@\"\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


@impls("D15: upstream ignores git return codes (commit_base succeeds silently)")
def test_d15_git_failure_is_loud(impl, tmp_path, monkeypatch):
    if impl == "framework":
        ws = fw_ws(tmp_path)
        commit_base = gating.commit_base
    else:
        import_upstream()
        from wikiskill import gating as ug
        ws = up_ws(tmp_path)
        commit_base = ug.commit_base
    _failing_git(tmp_path, monkeypatch, "commit")
    with pytest.raises(GitError):
        commit_base(ws, 1)


def test_d15_rollback_restores_base_exactly(tmp_path):
    from hermes_wikiskill.util import tree_digest
    ws = fw_ws(tmp_path)
    base = gating.commit_base(ws, 1)
    digest = tree_digest(gating.active_dir(ws))
    gating.apply_proposal(ws, {"action": "create", "name": "cand", "skill_md": "x",
                               "purpose_md": "p"})
    open(os.path.join(gating.active_dir(ws), "stray.pyc"), "w").write("junk")  # ignored-type file
    gating.rollback(ws, base)
    assert tree_digest(gating.active_dir(ws)) == digest
    assert gitutil.head(gating.active_dir(ws)) == base


def test_d15_rollback_failure_raises(tmp_path, monkeypatch):
    ws = fw_ws(tmp_path)
    base = gating.commit_base(ws, 1)
    _failing_git(tmp_path, monkeypatch, "reset")
    with pytest.raises(GitError):
        gating.rollback(ws, base)
