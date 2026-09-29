"""Prompts for the three agent roles (upstream prompts.py, paper Appendix E).

Changes: the maintainer manifest and proposer table carry TRAIN-only grader
feedback (D18); the proposer is told how Hermes one-shot runs decide to load a
skill (R1-qa Q3) so accepted skills are phrased as task conventions.
"""

from __future__ import annotations

import json
import os

INFERENCE_PREFIX = (
    "You are an agent completing a task in a sandbox directory. "
    "Follow the instructions precisely. Use your tools to inspect files and "
    "verify your work before finishing. Write every deliverable file exactly "
    "as specified. Your final message should briefly state what you did.\n\n"
)


def inference_prompt(task: dict, sandbox: str | None = None) -> str:
    anchor = ""
    if sandbox:
        anchor = (
            f"\n\nWORKING DIRECTORY: {sandbox}\n"
            "Read input files from and write ALL deliverables into the WORKING "
            "DIRECTORY above. Use absolute paths (prefix every path with it). "
            "Do not guess the directory — it is given. Do NOT explore, read, or "
            "modify anything outside the WORKING DIRECTORY (ignore git state and "
            "files above it). Verify the final file exists at its absolute path "
            "before finishing.\n"
        )
    return INFERENCE_PREFIX + f"TASK: {task['title']}\n\n{task['prompt']}" + anchor


def _feedback_lines(fb: dict | None, indent: str = "  ") -> list[str]:
    if not fb:
        return []
    out = [f"{indent}grader feedback ({fb.get('grader')}"
           + (f", file {fb['file']}" if fb.get("file") else "") + "):"]
    for k in ("expected", "got"):
        if k in fb:
            out.append(f"{indent}  {k}: {json.dumps(fb[k])}")
    if "exit_code" in fb:
        out.append(f"{indent}  script exit code: {fb['exit_code']}")
    return out


def trace_manifest(sampled: list[dict]) -> str:
    lines = []
    for t in sampled:
        lines.append(f"- {t['task_id']} (score={t.get('score')}): {t.get('title', '')}")
        lines.append(f"  trace: {t['path']}")
        lines += _feedback_lines(t.get("feedback"))
    return "\n".join(lines)


def maintainer_prompt(ws: str, it: int, sampled_traces: list[dict]) -> str:
    wiki = os.path.join(ws, "wiki")
    return f"""You are the Wiki Maintainer in a WikiSkill evolution loop (iteration {it}).

Load the `wikiskill-maintainer` skill and follow it exactly.

WORKSPACE: {ws}
- Wiki (read + edit): {wiki}/  (index.md, log.md, skill-impact.md, patterns/)
- Raw TRAINING traces to analyze this iteration (read only, immutable), with the
  grader's feedback (what the grader expected vs what the agent produced):

{trace_manifest(sampled_traces)}

Procedure:
1. Compare expected vs produced output first: the difference usually names the
   convention or step the agent got wrong. Then read the trace JSONL files above
   selectively (search for errors, read the last messages of failed runs) —
   traces can be very long; do not read every line.
2. Perform deep trace analysis: compare successful vs failed tasks, identify
   ACTION patterns and the concrete conventions the expected outputs follow
   (formats, ordering, filtering, rounding), check whether any active skill
   guidance was helpful.
3. Update the wiki incrementally — WRITE EARLY, edit later. Create/update
   pattern pages under wiki/patterns/, rewrite wiki/index.md (complete content,
   one line per pattern: [name](wiki/patterns/name.md): PROBLEM + ROOT CAUSE +
   FIX in one or two sentences), append your findings to wiki/log.md.
   Finish writing before you run out of turns.
4. Keep pattern pages 10-30 lines, concise, no duplicates. Do not copy task
   answers verbatim; record the general rule that produces them.
"""


def proposer_prompt(ws: str, it: int, train_results: list[dict]) -> str:
    wiki = os.path.join(ws, "wiki")
    rows = []
    for r in sorted(train_results, key=lambda x: (x.get("score") or 0, x["id"])):
        rows.append(f"- {r['id']} [{r.get('split', 'train')}] score={r.get('score')}: {r.get('title', '')}")
        if (r.get("score") or 0) < 1.0:
            rows += _feedback_lines(r.get("feedback"))
    table = "\n".join(rows)
    ppath = os.path.join(ws, "runs", "proposals", f"iter-{it:02d}.json")
    active = os.path.join(ws, "skills", "active")
    return f"""You are the Skill Proposer in a WikiSkill evolution loop (iteration {it}).

Load the `wikiskill-proposer` skill and follow it exactly.

WORKSPACE: {ws}
- Wiki: {wiki}/ (index.md, skill-impact.md, patterns/, log.md)
- Raw traces: {os.path.join(ws, 'raw', 'traces', f'iter-{it:02d}', 'train')}/ (full execution logs,
  read any trace you need via read_file)
- Active skills: {active}/

Training rollout summary this iteration (failed tasks show the grader's feedback):
{table}

Rules (from the paper's Appendix E.3):
1. Read wiki/index.md FIRST, then wiki/skill-impact.md (contains full content of
   rejected proposals — DO NOT repeat rejected approaches).
2. Read relevant pattern pages, then read at least 4 execution traces for failed
   tasks to diagnose root causes. Traces can be long — read selectively.
3. Decide: create (new skill), patch (existing skill), or no_action.
4. Write your proposal JSON to: {ppath}
   Create/patch skills under {active} ONLY via the proposal file; the harness
   applies it, validates it on the validation split, and records the outcome in
   skill-impact.md. WRITE EARLY — leave enough turns to finish the file.

How the task agent uses skills (Hermes one-shot runs): it sees only each skill's
name and one-line description, and loads a skill with skill_view ONLY when the
skill carries domain knowledge or project conventions it lacks for THIS task.
So: name the skill after the task domain, and put the words the task prompts use
(e.g. the report/format name) in the FIRST ~60 characters of the `description`
(Hermes truncates the index line there; ≤ 200 chars in total), state the
concrete conventions as numbered rules with a short worked example, and do not
write generic process advice. Skill names: letters, digits, `-` or `_` only.

Proposal JSON schema (write the file with write_file):
{{"action": "create", "name": "skill-name", "skill_md": "full SKILL.md with YAML frontmatter (name, description) + When to Apply + When NOT to Apply + Instructions", "purpose_md": "Origin + Patterns Addressed + Evolution History"}}
{{"action": "patch", "name": "existing-skill", "edits": [{{"op": "append"|"replace"|"insert_after", "target": "exact text", "content": "..."}}]}}
{{"action": "no_action"}}
Prefer patching over creating when a skill is partially correct. Keep skills
concise and actionable. If no change is warranted, write {{"action": "no_action"}}.
"""


def gate_outcome_entry(ws: str, it: int, proposal: dict, r_val: float | None,
                       accepted: bool, diff: str, baseline_r: float | None,
                       note: str | None = None) -> str:
    prop_path = os.path.join(ws, "runs", "proposals", f"iter-{it:02d}.json")
    head = (f"### iter-{it:02d} — {'ACCEPTED' if accepted else 'REJECTED'} "
            f"(R_val={r_val}, R_best={'—' if baseline_r is None else baseline_r})")
    if proposal.get("action") == "no_action":
        return f"{head}\n\nProposal: no_action (no validation run performed).\n"
    if note:
        return (f"{head}\n\nProposal rejected before validation: {note}\n\n"
                f"```json\n{json.dumps(proposal, indent=2)[:4000]}\n```\n")
    name = proposal.get("name", "?")
    action = proposal.get("action", "?")
    body = [head, "", f"Proposal: {action} `{name}`",
            f"Proposal file: `{prop_path}` (full content embedded below)"]
    if diff.strip():
        body += ["", "```diff", diff.strip(), "```"]
    body += ["", "Full proposal content (paper: rejected proposals must remain visible to future proposers):",
             "", "```json", json.dumps(proposal, indent=2), "```"]
    if accepted:
        body += ["", f"Validation: {r_val} > R_best → accepted, skills committed."]
    else:
        body += ["", f"Validation: {r_val} ≤ R_best → skills rolled back; wiki retained."]
    return "\n".join(body)
