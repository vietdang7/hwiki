"""Agent runner facade: one rollout of the workspace's Hermes profile.

Returns the legacy dict consumed by gating (cmd, exit_code, duration_s,
stdout_path, session_file, dry_run) plus the visibility/usage extras.
"""

from __future__ import annotations

import os

from .backends import hermes


def run_agent(ws: str, prompt: str, *, tag: str, toolsets: str | None = None,
              model: str | None = None, max_turns: int | None = 15,
              run_budget: int | None = 300, workdir: str | None = None,
              include_framework: bool = False, dry_run: bool = False,
              skill_dirs: list[str] | None = None, run_dir: str | None = None,
              home: str | None = None, copy_skills: bool = False, role: bool = False) -> dict:
    if skill_dirs is None:
        skill_dirs = hermes.active_skill_dirs(ws, include_framework=include_framework)
    res = hermes.run(ws, prompt, run_dir=run_dir or os.path.join(ws, "runs", tag), home=home,
                     skill_dirs=skill_dirs, toolsets=toolsets, model=model,
                     max_turns=max_turns, run_budget=run_budget, workdir=workdir,
                     dry_run=dry_run, copy_skills=copy_skills, role=role)
    return {"cmd": res.cmd, "exit_code": res.exit_code, "duration_s": res.duration_s,
            "stdout_path": res.stdout_path, "session_file": res.session_file,
            "dry_run": res.dry_run, **res.extra}
