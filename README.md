# hermes-wikiskill

`hermes-wikiskill` (short alias `hws`) runs the **WikiSkill** self-evolution loop
(arXiv:2608.27454) on a [Hermes Agent](#requirements) host.

The loop has four steps:
1. The agent works on graded tasks.
2. A *maintainer* distills the traces into a persistent wiki.
3. A *proposer* writes one candidate skill.
4. A validation gate keeps the skill **only if** `R_val > R_best`, and rolls it back otherwise.

It is derived from upstream `wikiskill` 0.1.5 (commit `cdc291c`, MIT; see `NOTICE`
and `LICENSE`). It keeps upstream's algorithm and verbs, and fixes the defects that
stop upstream from working on Hermes v0.21.3. Only the Hermes backend is shipped;
upstream's Claude, Codex and Copilot backends were dropped.

> Tiếng Việt (có phần hỏi đáp): [README.vi.md](README.vi.md).
> Installing with a Hermes agent, step by step: [INSTALL.md](INSTALL.md).

## What changed versus upstream, and why

Each row is a defect in upstream 0.1.5, verified from source. Every fix is pinned by
the test suite in `tests/` (the `wikiskill==0.1.5` test dependency is imported only to
show each defect failing on upstream).

| ID | Upstream behaviour | hermes-wikiskill |
|---|---|---|
| D1 | Rollouts ran with `-t terminal,file,code_execution`, so Hermes never listed the active skills (`<available_skills>` is built only when a skills tool is enabled). **The gate could not accept anything.** | Every rollout enables the `skills` toolset. After each rollout the framework checks the exported session prompt and **fails loudly (exit 3)** if the active skills are not visible. |
| D2 | A dry-run wrote a `score:null` trace. The next real run then crashed ("trace already exists"). | Dry-run never persists anything, and real traces are never overwritten silently. |
| D3 | `evolve --dry-run` wrote fake state, wiki entries and a git commit. | `--dry-run` writes nothing. A test hashes the workspace before and after. |
| D4 | Fixes lived as hand patches in site-packages, and `pip install -U` reverted them. | The fixes are in the source of a separate distribution (`hermes-wikiskill`). `doctor` re-verifies them after upgrades. |
| D5 | The whole `.env`, `auth.json` and `config.yaml` (bot tokens, every API key) were copied into each workspace and never refreshed. | Only the model's provider key is copied. It is refreshed each run, stored `chmod 600`, never logged, and the config written has no inline key. |
| D6 | A fixed flag set gave `unrecognized arguments` on older Hermes. | Flags are detected from `hermes chat --help`, with a tested legacy fallback. |
| D7 | A failed Hermes launch was graded as an ordinary 0.0. | A launch failure is an error, not a score. |
| D8 | There was no held-out split, and upstream's demo val baseline was already 0.8889 (nothing left to learn). | A `test` split is used only by `eval` and never given to the maintainer, proposer or gate; a role that touches it voids its iteration. The new `house-report` bench leaves headroom for learning. |
| D9 | There was no path from an accepted skill to a real profile. | `promote` (backup, receipt, collision refusal, owner rule) and `demote` (byte-identical restore; if the profile's skills dir changed in between, e.g. its first Hermes chat or other skills' usage counters, the receipt says `identical: false`). |
| D10–D13 | Sandbox or proposal paths could escape their directories; `code_stdout` ignored the exit code; `bench` kept stale sandboxes. | Contained paths, exit 0 required, sandboxes re-materialized. |
| D14 | There was no workspace lock. | `flock` on `<ws>/.hws.lock` (exit 75 when busy), `--max-parallel 1`, plus the `HWS_ROLLOUT_LOCK` env var for one rollout at a time. |
| D15 | Git return codes were ignored. | Every git call is checked, and the tree hash is verified before state is saved. |
| D16 | `compare` paired tasks by ID only and overwrote earlier results. | Task-definition hashes are compared, and comparison ids are unique. |
| D17 | A profile's `disabled_toolsets` could silently remove `skills` again. | The `doctor` check `profile-toolsets`, plus the per-run visibility check. |
| D18 | The maintainer never saw grader feedback. | TRAIN-only grader feedback (expected vs. got) is added to trace metadata. Val and test answers are never exposed. |

## Requirements

- Hermes Agent (tested with v0.21.3) with `hermes` on `PATH`.
- Python ≥ 3.10 with `venv` (`sudo apt install python3-venv` on Ubuntu).
- Linux (macOS works for development and tests). The reference host had 2 vCPU and
  about 2 GB RAM; the framework runs one rollout at a time, so a small VPS is enough.
- A working provider key in the Hermes profile you want to borrow credentials from
  (default: `default`). Only that provider's key is copied, nothing else.
- No runtime Python dependencies beyond the standard library.

## Install

On the machine where Hermes runs:

```bash
git clone https://github.com/vietdang7/hwiki.git ~/ops/hermes-wikiskill-src
bash ~/ops/hermes-wikiskill-src/install.sh --dry-run   # optional: checks + plan, writes nothing
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill
```

The clone location is only a convention used by the commands in this README; any
directory works. See [INSTALL.md](INSTALL.md) for a copy-paste prompt that lets a Hermes
agent do the install for you.

`install.sh` does the following, in order:

1. **Preflight.** Checks Python, finds `hermes`, and reads its version from package
   metadata (never `hermes --version`, which writes into `~/.hermes`). It checks the
   `chat` flags under a throwaway `/tmp/hws-*` home, and checks disk space and RAM.
2. **Package.** Creates a venv at `~/ops/hermes-wikiskill/venv` and installs the
   package there with a non-editable pip install.
3. **Commands.** Creates the symlinks `~/.local/bin/hermes-wikiskill` and
   `~/.local/bin/hws`. It refuses to replace anything there that it does not own.
4. **Profile.** Runs `hws setup-profile` to create or refresh the dedicated Hermes
   profile **`wikiskill`**. That profile has no gateway, no messaging tokens, only
   the provider key, the skills toolset enabled, and the operator skill
   `hermes-wikiskill`.
5. **Check.** Runs `hws doctor --json --profile wikiskill`, prints a summary, and
   appends to `~/ops/hermes-wikiskill/install.log`.

Options:

| Flag | Meaning |
|---|---|
| `--yes` | Don't ask for confirmation. Required when stdin is not a terminal. |
| `--prefix DIR` | Install root. Default `~/ops/hermes-wikiskill`. With another prefix, run `hws` commands with `HWS_HOME=<prefix>` so they find its workspaces. Receipts and backups of `setup-profile`, `promote` and `demote` are found via the installed launcher's own prefix, and `HWS_HOME` overrides that. |
| `--profile NAME` | Profile to create. Default `wikiskill`. |
| `--source-profile NAME` | Profile the provider key is read from. Default `default`. |
| `--dry-run` | Preflight and plan only; writes nothing. |
| `--i-am-owner` | Owner approval for an **existing** profile that hermes-wikiskill did not create (required for `default`). It installs **only** the operator skill into that profile's `skills/`, after backing up any existing copy, and writes a receipt. The profile's `config.yaml`, `.env`, `auth.json` and `SOUL.md` are never touched. Undo: `uninstall.sh`, or `hws setup-profile --profile NAME --remove --i-am-owner`. See [Owner approval](#owner-approval-the-default-profile). |

The install is idempotent. A second run with the same source prints
`no changes` and exits 0.

## Upgrade

Pull the new source and run the installer again:

```bash
git -C ~/ops/hermes-wikiskill-src pull
bash ~/ops/hermes-wikiskill-src/install.sh --yes
~/.local/bin/hws doctor --profile wikiskill
```

The package is reinstalled only when the source changed. Workspaces, receipts and
the profile's skills are kept. Upgrading the upstream `wikiskill` package does not
affect this framework, because it is a separate distribution with its own venv.

## Uninstall

```bash
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes            # keep workspaces, receipts, diagnostics
bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes --purge    # also delete them
bash ~/ops/hermes-wikiskill-src/uninstall.sh --dry-run        # show the plan only
```

The uninstaller works like this:

- It works only on a prefix that holds the installer's `install-state.json`.
  Without that file it refuses to touch a venv or workspaces there. It also refuses
  `/`, `$HOME` (or anything above it), and any prefix that overlaps `~/.hermes` or
  `~/ops/wikiskill`. `install.sh` applies the same prefix rules.
- It handles **every** profile any install run set up. `install-state.json` keeps them all in
  `profiles[]`, merged across runs, so the latest `--profile` does not matter. It also handles every
  profile that still has an un-removed operator-skill receipt under `<prefix>/receipts/`.
  `--profile NAME` is only a check: a name the install never set up is refused.
- It writes a timestamped backup to `~/ops/hermes-wikiskill-backups/<ts>/`: the
  receipts, `diagnostics/` and the install log, plus a tarball of our own profile. With
  `--purge` it also adds `workspaces.tgz`. The backup is owner-only because the profile
  `.env` holds a key. `diagnostics/` holds operator-only files that name the readable
  out-of-workspace paths of voided role runs; the workspace itself records only an id.
- Our own profiles are removed. The uninstaller then waits 90 s and verifies that they are
  still absent. A profile counts as ours when its `.hws-profile.json` names that profile and
  its path. A copy made by `hermes profile create --clone-all` carries our marker for another
  profile, so it counts as foreign and is kept. A marker from before v0.2.0 (no name/path)
  counts only when `install-state.json` lists the profile.
- A profile we did not create, including `default`, is **never** removed. Only the
  operator skill that `--i-am-owner` installed into it is undone, through
  `hws setup-profile --profile NAME --remove --i-am-owner`: the backup is restored,
  or the skill directory is deleted. Every receipt chain is checked (`--dry-run`) before
  anything changes. The uninstaller is never stuck:
  - If you deleted the skill directory, or put your own earlier copy back, the receipts are
    marked undone.
  - If you edited the skill, the edited copy is first saved to an owner-only directory
    `<prefix>/backups/edited-<profile>-<ts>/`. That path is printed and recorded in the
    receipt. The uninstaller then stops with nothing else removed. Run the same command
    again to finish; the saved copy stays.
  - Exit 4 (`uninstall.sh`: exit 1) is left for three cases where you decide. Nothing else is
    changed, and the message names the step that unblocks it (Troubleshooting has one row each):
    - the skill was edited (`matches no receipt`): the copy is saved as above; run the same
      command again;
    - a symlink or a plain file stands at the skill path (`is not a plain directory`): nothing is
      followed or changed. Remove it (`rm -rf <path>` removes a link, never what it points to),
      or move it aside with `mv`, then run the same command again;
    - the backup of your earlier copy is missing (`backup … is missing`): `rm -rf <skill dir>`,
      then run the same command again.
- It then removes the venv, the two symlinks and the install log/state files.
- It never runs `hermes profile delete` and never touches gateways.

## Usage

Workspaces live in `~/ops/hermes-wikiskill/workspaces/<ws>`. Change the root with
`--ws-root DIR` or the `HWS_HOME` env var.

```bash
hws version
hws doctor --profile wikiskill            # add --live for one real rollout (costs one LLM call)

hws init eff1                             # workspace with the bundled house-report train/val tasks
hws init mine --tasks /path/to/tasks.json # your own tasks (upstream schema, splits train/val only)
hws status eff1                           # baseline, R_best, next_iter, history, active skills
hws status eff1 --json

hws evolve eff1 --iters 0                 # baseline only: validation with no skills
hws evolve eff1 --iters 3                 # iterations next_iter..3: train -> maintain -> propose -> gate
hws evolve eff1 --iters 3 --dry-run       # print the plan, write nothing
hws run-task eff1 <task_id>               # one rollout

# held-out evaluation (the test file never enters the workspace)
hws eval eff1 --tasks ~/ops/hermes-wikiskill-src/benches/house-report/test.json --skills empty  --repeat 2 --tag test-empty
hws eval eff1 --tasks ~/ops/hermes-wikiskill-src/benches/house-report/test.json --skills active --repeat 2 --tag test-active

hws report eff1 --html /tmp/hws-dashboard.html   # single offline HTML file (light/dark)
hws report eff1 --json
```

Run the held-out eval (or `measure.sh`) only **after** you have finished evolving that workspace.
Once `<ws>/eval/` exists, a later maintainer can stumble on it, even with a broad `search_files`,
and that iteration is voided. To keep evolving, start a fresh workspace with `hws init`.

Upstream's other verbs are kept: `bench`, `gate`, `maintain`, `propose`, `reset`,
`compare`, `transfer`. Every mutating verb takes the workspace lock, and accepts
`--dry-run` and `--wait` (block instead of exiting 75).

### The bundled experiment

`benches/house-report/` holds a deterministic bench (8 train, 6 val, 10 held-out
test tasks). Its graders encode house conventions that the model can only learn
from graded experience. The three scripts below drive the whole measurement:

```bash
B=~/ops/hermes-wikiskill-src/benches/house-report
bash $B/run-effectiveness.sh eff1        # init, baseline (abort if >= 0.5), evolve <= 4 iters, freeze, measure
bash $B/measure.sh eff1 --reuse          # recompute eval/effectiveness.json from existing summaries (no LLM cost)
bash $B/measure.sh eff1 --tag-prefix qa- # an independent re-measurement under new tags
bash $B/stock-compare.sh                 # the same bench on stock PyPI wikiskill==0.1.5
```

### What the acceptance suite compares

`tests/acceptance/` runs the same 18 check ids against stock upstream, the current
install and this framework. Some checks need a target-specific procedure:
- **AC01** installs or locates each target in its own way.
- **AC04** and **AC07** work on a copy for `current`, which is read-only.
- **AC08** is N/A on stock and reinstalls the framework.
- **AC12** uninstalls stock with pip and is N/A on `current`.

On stock and `current`, **AC10** (doctor), **AC15** (promote/demote) and **AC17**
(held-out split) are capability-presence checks: they FAIL or are N/A because the
feature does not exist there. They are not the same measurement as on the framework.

Hermes always lists its essential `hermes-agent` skill, so it appears in
`skills_listed` of both eval arms (empty and evolved). Only the evolved arm lists the
workspace's skills.

### Talking to the operator skill

The `wikiskill` profile carries the operator skill `hermes-wikiskill`. It teaches
the agent to answer from real command output: `hws status`, `hws report`, how to
read `state.json`, and the owner-approval rule.

```bash
hermes -p wikiskill chat -Q -q "What is the state of wikiskill workspace eff1?"
```

### Promote / demote

Accepted skills change normal Hermes chats **only after** they are promoted into a
profile. Always review with `--dry-run` first:

```bash
hws promote eff1 --profile hws-test-demo --dry-run
hws promote eff1 --profile hws-test-demo            # copies gate-ACCEPTED skills, backup + receipt
hws demote --profile hws-test-demo --last           # restores the skills dir byte for byte
hws demote --profile default --last --i-am-owner    # the same undo for an owner promote into `default`
```

`promote` behaves as follows:
- It copies only skills accepted by the gate.
- It refuses a name that already exists in the target profile unless you pass
  `--replace`.
- It refuses any profile that hermes-wikiskill did not create (`default`,
  any other existing profile) unless you pass `--i-am-owner`. `hws-test-*` profiles and
  our own profile need no flag. `demote` follows the same rule.
- It refuses skills that contain symlinks (a link could pull a secret file such as
  `~/.hermes/.env` into the profile).
- It writes a receipt to `~/ops/hermes-wikiskill/receipts/`.

## Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| exit **3** | The active skills were not visible to the agent in that rollout (D1/D17). The rollout was not scored and the gate made no decision. `doctor` checks the operator profile, not the rollout, so look at the rollout itself: `<ws>/runs/.../run.json` (`skills_expected` vs `skills_visible`, `skills_index_present`) and the saved `session.jsonl` (its `<available_skills>` block). Common causes: the skill's own frontmatter hides it (`platforms`, `requires_*` conditions); the directory name differs from the frontmatter `name`; a Hermes upgrade changed the index format. |
| exit **4** | Hermes is incompatible (missing required flags, or it is unusable). Run `doctor` and look at `hermes-version` and `hermes-flags`. |
| exit **75** | Another process holds `<ws>/.hws.lock`; the message names the pid. Wait, or pass `--wait`. Do not delete the lock file. |
| exit 2 | Usage error. See `hws <verb> --help`. |
| `no changes` from install.sh | Expected on a re-run: same source, profile up to date, doctor OK. |
| install.sh: `refusing to overwrite ~/.local/bin/hws` | Something else owns that name. Remove or rename it yourself; the installer never overwrites foreign files. |
| install.sh: `doctor reported failing checks` | The failing check ids are printed above that line and logged to `~/ops/hermes-wikiskill/install.log`. |
| install.sh / uninstall.sh: `refusing prefix …` (exit 2) | The prefix is `/`, `$HOME` or above it, or overlaps `~/.hermes` / `~/ops/wikiskill`. Pick another `--prefix`. |
| install.sh: `exists but … install-state.json does not` | That venv was not created by this installer. Pick another `--prefix`. |
| uninstall.sh: `install-state.json is missing` | Nothing there was installed by `install.sh`; it refuses to delete it. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `matches no receipt` | The operator skill in a foreign profile was edited after we installed it. Your edited copy was just saved to the owner-only dir the message prints (`<prefix>/backups/edited-…`); nothing else changed. Run the same command again (`bash ~/ops/hermes-wikiskill-src/uninstall.sh --yes`, or `hws setup-profile --profile NAME --remove --i-am-owner`): it restores or removes the skill per the receipts. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `is not a plain directory` | A symlink or a plain file stands where the operator skill dir of a foreign profile was (for `default`: `~/.hermes/skills/hermes-wikiskill`). Nothing was followed or changed. Remove it with the `rm -rf <path>` the message prints (for a symlink this removes only the link, not what it points to), or move it aside (`mv <path> <somewhere else>`) to keep it. Then run the same command again: the receipts are marked undone. An earlier copy that was backed up is not put back; it stays in `<prefix>/backups/`. |
| uninstall.sh / `setup-profile --remove`: exit **4** (uninstall.sh: exit 1), `backup … is missing` | The backup of the copy that was there before hermes-wikiskill cannot be found. If you do not need that copy, `rm -rf` the skill dir the message names, then run the same command again. |
| uninstall.sh: profile dir `reappeared` | Something (a gateway or the dashboard) still writes that profile. Investigate; the uninstaller does not retry blindly. |

For all three exit-4 rows, `uninstall.sh` prints the same first line (`changed since it was installed, or its
backup is missing`) with the `hws` message in parentheses; the text in the parentheses picks the row.

The `doctor` check ids are:
`python`, `hermes-binary`, `hermes-version`, `hermes-flags`, `profile`,
`profile-toolsets`, `profile-credentials`, `operator-skill`, `framework-skills`,
`skills-visibility`, `workspace-locks`, `disk`, `ram`, `live-run` (only with
`--live`) and `pass-env` (a warning, only when `HWS_PASS_ENV` is set; that switch
is for test doubles). `ok: true` means no check failed; `warn` entries are worth
reading.

## Honest expectations

- Gains appear only on **repeated, graded task families**. A skill learned on one
  bench does not make everyday chat smarter by itself.
- A rejected proposal or a `no_action` iteration is the gate working correctly.
- **The maintainer and proposer are watched, not jailed.** They run as your Unix
  user. Their confinement is detection: the iteration becomes invalid, with no
  gate run and the skills rolled back, if a session does any of these:
  - types a path outside the workspace;
  - touches an eval or held-out file;
  - uses the sealed val hashes as an oracle.

  It is not an OS sandbox. Keep held-out test files and bench generators out of
  workspaces.
- **Known detector limitations** (documented, not fixed in this release):
  - *False positive:* a maintainer that greps an in-workspace train trace which quotes a path
    outside the workspace voids the iteration.
  - *False positive:* a local git identity written into `wiki/.git/config` voids the iteration,
    because it counts as a git-metadata change.
  - *Miss:* parent or root access written only inside code-string literals, as `~user`, or with
    `\/` escapes is not flagged by the out-of-workspace detector.

  A void costs one iteration; a miss is why the held-out test number, not R_val, is the result
  to trust.
- **Where a void's flagged paths go.** Inside the workspace, a voided iteration records the flagged
  out-of-workspace paths only as digests. The history, the wiki notes and `confinement.json` hold digests,
  so a later role that reads those notes is not voided again. The readable paths are for the operator only,
  in `<prefix>/diagnostics/<ws-id>/`.
- **What happens to a bad maintainer or proposer run.** If a role run is voided, raises, or is killed and
  resumed with `hws reset` or the next `evolve`, the whole workspace is put back to its state before that
  run, file by file. In the run's own directory, only the records the harness wrote itself are kept (it
  checks each against a sha256 kept outside the workspace); anything the role wrote there is removed.
- **The retro-audit can report `laundering-attempt`.** `python -m hermes_wikiskill.heldout <ws>` gives this
  verdict to a later role run whose tool calls name a file that an earlier bad run's cleanup removed, and
  then exits 1. It only reports: the cleanup has already removed that file, and no gate or eval result
  changes.
- **What undoing the operator skill restores.**
  - Restored: your earlier copy of `hermes-wikiskill`, with its files, symlinks and file/dir
    modes (group-write included).
  - Removed, when empty afterwards: `~/.hermes/skills/` and `~/.hermes/profiles/`, if they did
    not exist before.
  - Not restored: timestamps and file ownership.
  - Not brought back: an earlier copy whose skill dir you deleted, or replaced with a link or a
    file, yourself. Its backup stays in
    `<prefix>/backups/`, and so does every edited copy saved on the way.
- **Trust the held-out test number over R_val.** The sealed val hashes can still
  act as an oracle, and the detector for that is a heuristic. A determined
  maintainer could partly tune a skill to the val split. The held-out test
  split cannot influence the result: it is never given to maintainer,
  proposer or gate, and a role that touches it voids its iteration. So its
  number is the unbiased one.
- Measured on a reference host (Hermes v0.21.3, `deepseek-v4-flash`) with the
  bundled house-report bench (held-out test, evolved vs. empty skills, ≥ 2 repeats
  each). The evolved skill set scored **1.0**
  in both repeats and the empty skill set **0.0** in both, so **Δ = +1.0** on 10
  held-out tasks.
  - One skill was accepted (val 0.0 → 1.0), and all 20 evolved test sessions
    loaded it with `skill_view`.
  - No evolved-arm rollout escaped its sandbox. One empty-arm rollout did: it was
    flagged, scored 0 and excluded from Δ-excluding-flagged, which is also +1.0.
  - Two earlier runs gave the same Δ.

  This is a narrow, synthetic result: it shows the loop can learn a convention the
  model cannot guess, not that everyday chat gets smarter. Reproduce it with
  `benches/house-report/run-effectiveness.sh` (see below).
- Cost: roughly US$0.003 per rollout on `deepseek-v4-flash` (estimate). One
  iteration is about one rollout per train task, plus a maintainer run, plus a
  proposer run, plus one rollout per val task. The real spend is in
  `hws report <ws> --json` → `costs` (that workspace only: its rollouts,
  maintainer/proposer runs and evals). The reference run cost:
  - evolve (baseline + one iteration): US$0.096;
  - held-out eval (40 rollouts): US$0.128;
  - total: US$0.224.

  An average test rollout took about US$0.002 and 12 s with the evolved skill, and
  US$0.004 and 38 s without skills.
- Nothing learned is active in daily use until two things happen: a gate
  **ACCEPT**, then an owner-approved **promote**.

## Owner approval: the default profile

Nothing is installed into the live `default` profile by the installer's defaults.
When the owner has reviewed the evidence (the skill text, the gate result and the
held-out delta), these commands enable it:

```bash
# 1. operator skill only -> ~/.hermes/skills/hermes-wikiskill (never config.yaml/.env/auth.json)
bash ~/ops/hermes-wikiskill-src/install.sh --yes --profile default --i-am-owner

# 2. review, then promote the accepted skills of a workspace
hws promote eff1 --profile default --dry-run
hws promote eff1 --profile default --i-am-owner

# undo, in reverse order
hws demote --profile default --last --i-am-owner                  # 2: restores the skills dir from its backup
hws setup-profile --profile default --remove --i-am-owner         # 1: restores or deletes the operator skill
```

Step 1 writes a backup of any existing `~/.hermes/skills/hermes-wikiskill` and a
receipt under `<prefix>/receipts/` (default `~/ops/hermes-wikiskill/receipts/`).
`uninstall.sh` undoes step 1 for you, whatever `--profile` the last install run used: it
undoes every un-removed operator-skill receipt:
- A deleted skill dir, or your own earlier copy put back, counts as already undone.
- An edited skill is first saved to `<prefix>/backups/edited-default-<ts>/` (owner-only).
  The command then exits 4 and `uninstall.sh` stops before removing anything else. Run the
  same command again and it finishes.

`hws setup-profile --remove` also exits non-zero when our operator skill is present but no
receipt is found. With a custom `--prefix`, run it with
`HWS_HOME=<prefix>`. `uninstall.sh` does **not** undo step 2: run the `demote` first.

## License

MIT. Derived from upstream wikiskill (commit `cdc291c`); attribution is in
`NOTICE`.
