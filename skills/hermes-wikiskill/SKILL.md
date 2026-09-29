---
name: hermes-wikiskill
description: "Operate hermes-wikiskill (WikiSkill on Hermes): workspace state, evolve and gate results, learned skills, evals, costs, doctor, promote. Load for any wikiskill question."
version: 0.2.0
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [wikiskill, hermes-wikiskill, skills, evolution, operator]
---

# hermes-wikiskill operator

`hermes-wikiskill` (short alias `hws`) runs the WikiSkill loop on this Hermes
host. The loop is: task rollouts → a maintainer distills a wiki → a proposer
writes one candidate skill → a validation gate keeps it **only if**
`R_val > R_best`, otherwise it rolls back. It is derived from upstream
`wikiskill` 0.1.5 (MIT) and fixes its Hermes defects.

Answer the user in their own language. Every
number you report must come from a command output or file below. Never
estimate a score, a cost or a skill name from memory.

## Where things live

- Tool home: `~/ops/hermes-wikiskill` (`$HWS_HOME`; an install with another
  `--prefix` needs `HWS_HOME=<prefix>`). Workspaces:
  `~/ops/hermes-wikiskill/workspaces/<ws>`. Receipts:
  `~/ops/hermes-wikiskill/receipts/`.
- Inside a workspace `<ws>`:
  - `tasks.json`: the train and val tasks. Held-out test tasks are never
    stored here.
  - `runs/state.json`: evolve state (see below).
  - `skills/active/<name>/SKILL.md`: the skills the gate has **accepted**.
  - `wiki/log.md`, `wiki/skill-impact.md` and `wiki/patterns/`: the evidence
    behind every proposal, including rejected ones.
  - `raw/traces/iter-NN/<split>/<task>.meta.json`: per-rollout score, usage
    and cost, whether the skills were visible, and `skill_views`.
  - `eval/<tag>/summary.json`: held-out evaluations.
- Workspace runs use their own Hermes home (`<ws>/.hermes-home`). The skills
  there are **not** the skills of `default` or any other profile.

## Answer "what is the state of workspace X?"

Run these, read-only and without any LLM cost, then summarize:

```bash
hws status X --json          # baseline, r_best, next_iter, history, active skills
hws report X --json          # adds evals (empty vs evolved), acceptance, costs
ls ~/ops/hermes-wikiskill/workspaces     # when the user does not name a workspace
```

If `hws` is not on PATH, use `~/.local/bin/hermes-wikiskill`. If a flag is
rejected, check `hws <verb> --help` and do not guess.

Read `runs/state.json` like this:

- `baseline` is the validation score with **no** skills (S0).
- `r_best` is the best validation score accepted so far. The gate compares
  against it.
- `next_iter` is the next iteration number.
- `history[]` has one entry per gated iteration: `iter`, `train_mean`,
  `r_val`, `accepted` (true = ACCEPT, false = rejected and rolled back),
  `proposal`, and, from the framework, `r_best_before`, `skills_visible`
  and `ts`.
- An iteration where the proposer chose `no_action` only advances
  `next_iter`, so it has no `history` entry. Therefore
  `len(history) <= next_iter - 1`.
- `skills_visible: false`, or exit code 3, means the skills were not shown to
  the agent. That result says nothing about the skill's quality. Report it as
  a setup fault, never as "the skill did not help".

"Skills learned" means the directories in `skills/active/` of that
workspace, not proposals. Rejected proposals are listed in
`wiki/skill-impact.md`.

## Health: `doctor`

```bash
hws doctor --json            # checks: python, hermes-*, profile*, skills-visibility, disk, ram ...
hws version --json
```

`ok: true` means no check failed. `warn` entries, such as low RAM, are worth
mentioning. Never paste secret values: doctor details never contain them,
and nothing else you print should either.

## Running work (costs money and time — only when the owner asks)

```bash
hws run-task X <task_id>                  # one rollout
hws evolve X --iters 2                    # train → maintain → propose → gate, per iteration
hws eval X --tasks <test.json> --skills active --repeat 2
hws eval X --tasks <test.json> --skills empty  --repeat 2
hws evolve X --iters 2 --dry-run          # prints the plan, writes nothing
```

- Before a paid run, say what it will do and that it costs money. One
  iteration is roughly one rollout per train task, plus a maintainer run,
  plus a proposer run, plus one rollout per val task. Each rollout costs
  about US$0.003 with a cheap model such as `deepseek-v4-flash` (it
  depends on the provider and model of the source profile).
- Real spend is in `hws report X --json` → `costs`. Quote that, not the
  estimate.
- Run **one rollout at a time**: rollouts are serialized by design (small
  hosts, e.g. 2 vCPU / 2 GB RAM, cannot run them in parallel). Never start
  a second evolve or eval in parallel.
- Exit code 75 means the workspace is busy (another run holds the lock).
  Wait; do not delete lock files.
- Exit code 4 means Hermes is incompatible. Run `doctor` and report what it
  says.
- Exit code 3 means the skills were invisible (see above).
- Evaluate on held-out test tasks only **after** evolve is finished. Never
  give test tasks to evolve, maintain, propose or gate.

## OWNER-APPROVAL rule: promote / demote

Accepted skills affect normal Hermes chats **only after** they are promoted
into a profile. Adopting a skill into a live profile requires the owner's
review and approval.

- **Never** run `promote` or `demote` without `--dry-run` against
  `default` or any other live profile (any profile with a gateway, or one
  the owner uses). Never add `--i-am-owner` or `--replace` yourself.
- What you may do:
  1. Show the evidence: the skill text in `skills/active/<name>/SKILL.md`,
     its gate result (`r_val` vs `r_best_before`), and the held-out eval
     delta if one exists.
  2. Run the read-only plan: `hws promote X --profile default --dry-run`.
  3. Hand the owner the exact command to run themselves, for example:
     `hws promote X --profile default --skills <name> --i-am-owner`.
     Undo is `hws demote --profile default --last --i-am-owner`. It restores the skills
     dir byte for byte from the backup, and a receipt is written to
     `~/ops/hermes-wikiskill/receipts/`.
  4. The operator skill itself, if the owner installed it into `default`
     (or another existing profile) with `--i-am-owner`, is undone with
     `hws setup-profile --profile default --remove --i-am-owner`, and
     `uninstall.sh` undoes it too, whichever profile the last install used.
     If that command says no receipt was found while the skill is present,
     the install used another prefix: tell the owner to add `HWS_HOME=<prefix>`.
- `promote` refuses a name that already exists in the profile unless
  `--replace` is given. That is how it avoids clobbering the owner's
  existing skills. Mention it when asked about conflicts.

## Honest expectations

- Gains appear only on **repeated, graded** task families. A skill learned
  on one bench does not make everyday chat smarter by itself.
- A rejected proposal or a `no_action` iteration is the gate working
  correctly, not a failure.
- Nothing learned is active in daily use until two things happen: a gate
  ACCEPT, then an owner-approved `promote`. After that, Hermes' skills index
  lists the skill, and the agent loads it with `skill_view` when a task
  needs it.
- Upstream shipped 8 `SKILL.md` files:
  - The 2 loop roles, `wikiskill-maintainer` and `wikiskill-proposer`. These
    ship inside the package and are used only inside workspace runs.
  - The operator guide `wikiskill-evolve`, which this skill replaces.
  - 5 example skills from upstream's own runs. These are not installed:
    they would bypass the gate on the owner's own tasks.
