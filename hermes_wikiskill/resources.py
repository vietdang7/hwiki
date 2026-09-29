"""Locate packaged data: framework role skills, the operator skill, bundled benches.

Wheel: ``hermes_wikiskill/data/{skills,benches}`` (setup.py copies the repo's
``framework/skills`` and ``framework/benches`` there). Source/editable runs fall back
to ``<package parent>/{skills,benches}``.
"""

from __future__ import annotations

import os

from .backends.base import FRAMEWORK_SKILLS

PKG = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(PKG, "data")
OPERATOR_SKILL = "hermes-wikiskill"
DEFAULT_BENCH = "house-report"


def _first(*cands: str) -> str | None:
    return next((c for c in cands if os.path.exists(c)), None)


def framework_skills_root() -> str:
    return os.path.join(DATA, "skills")


def require_framework_skills(root: str | None = None) -> list[tuple[str, str]]:
    """Validate the packaged maintainer/proposer skills or raise loudly (upstream #29)."""
    root = root or framework_skills_root()
    found = []
    for name in FRAMEWORK_SKILLS:
        src = os.path.join(root, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            raise FileNotFoundError(
                f"framework skill {name!r} not found at {src!r}: this hermes-wikiskill install is "
                f"broken (maintainer/proposer turns would run unskilled). Reinstall the package.")
        found.append((name, src))
    return found


def operator_skill_dir() -> str | None:
    return _first(os.path.join(DATA, "skills", OPERATOR_SKILL),
                  os.path.join(os.path.dirname(PKG), "skills", OPERATOR_SKILL))


def bench_dir(name: str = DEFAULT_BENCH) -> str | None:
    return _first(os.path.join(DATA, "benches", name),
                  os.path.join(os.path.dirname(PKG), "benches", name))
