"""Wiki Layer skeleton + audit commits (checked, D15). Never rolled back — except a
contaminated maintainer update (QA-F7), which is discarded AND purged from git history."""

from __future__ import annotations

import os

from . import gitutil


def wiki_dir(ws: str) -> str:
    return os.path.join(ws, "wiki")


def commit(ws: str, message: str) -> None:
    w = wiki_dir(ws)
    if not os.path.isdir(os.path.join(w, ".git")):
        gitutil.git(w, "init", "-q")
    gitutil.git(w, "add", "-A")
    gitutil.git(w, "commit", "-q", "--allow-empty", "-m", message)


def ensure(ws: str) -> None:
    w = wiki_dir(ws)
    os.makedirs(os.path.join(w, "patterns"), exist_ok=True)
    fresh = False
    for f, content in {
        "index.md": "# Pattern Index\n\n_No patterns yet._\n",
        "log.md": "# Evolution Log\n\n_No iterations yet._\n",
        "skill-impact.md": "# Skill Impact\n\n_No proposals yet._\n",
    }.items():
        p = os.path.join(w, f)
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
            fresh = True
    if fresh or not os.path.isdir(os.path.join(w, ".git")):
        commit(ws, "wiki skeleton")


def append_skill_impact(ws: str, entry: str) -> None:
    with open(os.path.join(wiki_dir(ws), "skill-impact.md"), "a", encoding="utf-8") as f:
        f.write("\n" + entry + "\n")


def append_log(ws: str, line: str) -> None:
    with open(os.path.join(wiki_dir(ws), "log.md"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def head(ws: str) -> str | None:
    w = wiki_dir(ws)
    return gitutil.head(w) if os.path.isdir(os.path.join(w, ".git")) else None


def discard_since(ws: str, commit: str) -> None:
    """QA-F7: drop every wiki change after ``commit`` (committed or not) and purge the
    dropped objects, so held-out data a contaminated maintainer wrote is not left in
    ``git log -p`` for the next maintainer."""
    w = wiki_dir(ws)
    gitutil.git(w, "reset", "-q", "--hard", commit)
    gitutil.remove_nested_git(w)  # T11 m1: a nested repo hides its dir from status / clean
    gitutil.git(w, "clean", "-ffdxq")  # R2-03: git-ignored files too (a role's `.gitignore: *`)
    gitutil.git(w, "reflog", "expire", "--expire=now", "--all")
    gitutil.git(w, "gc", "-q", "--prune=now")
