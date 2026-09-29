"""Backend result type and framework-skill names (upstream backends/base.py, trimmed)."""

from __future__ import annotations

from dataclasses import dataclass, field

FRAMEWORK_SKILLS: tuple[str, ...] = ("wikiskill-maintainer", "wikiskill-proposer")


@dataclass
class RunResult:
    cmd: list[str]
    exit_code: int | None = None
    duration_s: float = 0.0
    stdout_path: str | None = None
    session_file: str | None = None
    dry_run: bool = False
    extra: dict = field(default_factory=dict)
