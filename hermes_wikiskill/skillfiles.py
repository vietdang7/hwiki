"""SKILL.md frontmatter helpers (Hermes indexes a skill by its frontmatter ``name``)."""

from __future__ import annotations

import os
import re

from . import mini_yaml

_FENCE = re.compile(r"\A﻿?---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
DESC_LIMIT = 200  # total cap; Hermes' index shows only the first ~60 chars (INDEX_DESC_VISIBLE)
INDEX_DESC_VISIBLE = 60  # measured on host (R1-qa C1 closed): "…report.txt: emit a byte-exac..."


def frontmatter(text: str) -> dict:
    m = _FENCE.match(text or "")
    if not m:
        return {}
    try:
        data = mini_yaml.loads(m.group(1))
    except Exception:  # noqa: BLE001 - malformed YAML behaves like Hermes: empty
        return {}
    return data if isinstance(data, dict) else {}


def skill_name(skill_dir: str) -> str:
    """Name Hermes will list: frontmatter ``name``, else the directory name."""
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8", errors="replace") as f:
            name = frontmatter(f.read()).get("name")
    except OSError:
        name = None
    return str(name).strip() if name else os.path.basename(os.path.normpath(skill_dir))


def normalize_skill_md(text: str, name: str) -> str:
    """Guarantee a ``---`` frontmatter whose ``name`` equals the skill directory.

    A proposer-written SKILL.md without a fence would be listed without a
    description; one whose ``name`` differs from its directory would make the
    visibility check and ``skill_view(<dir>)`` disagree.
    """
    text = (text or "").replace("\r\n", "\n")
    m = _FENCE.match(text)
    if not m:
        first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), name)
        desc = first[:DESC_LIMIT].replace('"', "'")
        return f'---\nname: {name}\ndescription: "{desc}"\n---\n\n{text.lstrip()}'
    body = m.group(1)
    lines = body.split("\n")
    out, seen = [], False
    for ln in lines:
        if re.match(r"^name\s*:", ln):
            out.append(f"name: {name}")
            seen = True
        else:
            out.append(ln)
    if not seen:
        out.insert(0, f"name: {name}")
    return "---\n" + "\n".join(out) + "\n---\n" + text[m.end():]
