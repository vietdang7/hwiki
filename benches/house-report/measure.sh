#!/usr/bin/env bash
# measure.sh <ws> — held-out effectiveness: evaluate the bench's test.json with
# the empty skill set and with the workspace's active (evolved) skills, same
# model and turn budget, N repeats each; write <ws>/eval/<prefix>effectiveness.json
# and print a markdown table. Every number comes from the two eval summaries.
#
#   measure.sh <ws> [--repeat 2] [--tag-prefix P] [--tasks FILE] [--max-turns 15]
#                   [--ws-root DIR] [--reuse]
#
# --reuse recomputes from existing <P>test-empty / <P>test-active summaries
# without running anything (no LLM cost). Without it, existing tags are refused.
# The JSON names the held-out file by basename + sha256 only (never its path).
# Sandbox escapes (score forced to 0) and non-zero rollout exits are listed per arm
# (flags + notes), never re-scored. Non-zero exits with a real session (turn cap,
# exit 1) are legit graded outcomes. delta_excluding_flagged drops every task with
# an escape in either arm from both arms; threshold met needs delta AND
# delta_excluding_flagged >= 0.15 (>= 2 repeats each). Only arm mismatches are problems.
set -Eeuo pipefail

BENCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPEAT=2
PREFIX_TAG=""
TASKS=$BENCH_DIR/test.json
MAX_TURNS=15
WS_ROOT=${HWS_WS_ROOT:-${HWS_HOME:-$HOME/ops/hermes-wikiskill}/workspaces}
REUSE=0
WS=""

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --repeat) REPEAT=${2:?}; shift ;;
    --tag-prefix) PREFIX_TAG=${2?}; shift ;;
    --tasks) TASKS=${2:?}; shift ;;
    --max-turns) MAX_TURNS=${2:?}; shift ;;
    --ws-root) WS_ROOT=${2:?}; shift ;;
    --reuse) REUSE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "measure.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    *) [[ -z $WS ]] || { echo "measure.sh: one workspace only" >&2; exit 2; }; WS=$1 ;;
  esac
  shift
done
[[ -n $WS ]] || { usage >&2; exit 2; }
[[ $REPEAT =~ ^[1-9][0-9]*$ && $MAX_TURNS =~ ^[1-9][0-9]*$ ]] || { echo "measure.sh: bad --repeat/--max-turns" >&2; exit 2; }
[[ $PREFIX_TAG =~ ^[A-Za-z0-9._-]*$ ]] || { echo "measure.sh: bad --tag-prefix" >&2; exit 2; }
die() { echo "measure.sh: $*" >&2; exit 1; }

HWS=${HWS_CLI:-$(command -v hws || echo "$HOME/.local/bin/hws")}
PY=${HWS_PY:-python3}
WS_DIR=$WS_ROOT/$WS
[[ -d $WS_DIR ]] || die "no workspace at $WS_DIR"
[[ -f $TASKS ]] || die "no tasks file $TASKS"
export HWS_ROLLOUT_LOCK=${HWS_ROLLOUT_LOCK:-$HOME/ops/.hws-team.lock}

for arm in empty active; do
  summary=$WS_DIR/eval/${PREFIX_TAG}test-$arm/summary.json
  if ((REUSE)); then
    [[ -f $summary ]] || die "--reuse: missing $summary"
  else
    [[ ! -e $summary ]] || die "$summary exists; pass a new --tag-prefix or --reuse"
  fi
done

if ((!REUSE)); then
  [[ -x $HWS ]] || die "hws not found (set HWS_CLI)"
  for arm in empty active; do
    echo "eval: $WS test split, skills=$arm, repeat=$REPEAT, max-turns=$MAX_TURNS (tag ${PREFIX_TAG}test-$arm)" >&2
    "$HWS" --ws-root "$WS_ROOT" eval "$WS" --tasks "$TASKS" --skills "$arm" --repeat "$REPEAT" \
      --tag "${PREFIX_TAG}test-$arm" --max-turns "$MAX_TURNS" --json >/dev/null \
      || die "hws eval failed for skills=$arm"
  done
fi

"$PY" - "$WS_DIR" "$PREFIX_TAG" "$TASKS" "$REPEAT" <<'PY'
import hashlib, json, os, sys

ws_dir, prefix, tasks_file, repeat = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])


def num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def mean(xs):
    return round(sum(xs) / len(xs), 6) if xs else None


def load(arm):
    path = os.path.join(ws_dir, "eval", f"{prefix}test-{arm}", "summary.json")
    with open(path, encoding="utf-8") as fh:
        return path, json.load(fh)


(empty_path, empty), (active_path, active) = load("empty"), load("active")
with open(tasks_file, "rb") as fh:
    tasks_sha = hashlib.sha256(fh.read()).hexdigest()

problems = []
if empty.get("skills") != "empty" or active.get("skills") != "active":
    problems.append("summary 'skills' fields are not empty/active")
for key in ("model", "max_turns", "hermes_version", "tasks_sha256"):
    if empty.get(key) != active.get(key):
        problems.append(f"arms differ in {key}: {empty.get(key)!r} vs {active.get(key)!r}")
for arm, s in (("empty", empty), ("active", active)):
    if s.get("tasks_sha256") not in (None, tasks_sha):
        problems.append(f"{arm} arm tasks_sha256 != sha256({tasks_file})")
    if len(s.get("repeats") or []) < repeat:
        problems.append(f"{arm} arm has {len(s.get('repeats') or [])} repeats < {repeat}")


def rep_means(s):
    return [r["mean"] for r in s.get("repeats") or [] if num(r.get("mean"))]


def results(s):
    for r in s.get("repeats") or []:
        for res in r.get("results") or []:
            yield r.get("rep"), res


notes = []


def arm_flags(arm, s):
    """R1-27: per-arm sandbox escapes and non-zero rollout exits (listed as notes, never re-scored)."""
    escaped = sorted(f"rep{rep}:{res.get('task_id')}" for rep, res in results(s) if res.get("sandbox_escape"))
    nonzero = sorted(f"rep{rep}:{res.get('task_id')}(exit {res.get('exit_code')})" for rep, res in results(s)
                     if res.get("exit_code") not in (0, None))
    escapes = max(len(escaped), s["escapes"] if num(s.get("escapes")) else 0)
    if escapes:
        notes.append(f"{arm} arm: {escapes} sandbox escape(s), scores forced to 0: {', '.join(escaped) or 'see summary'} "
                     "(excluded in delta_excluding_flagged)")
    if nonzero:
        notes.append(f"{arm} arm: {len(nonzero)} rollout(s) exited non-zero with a graded session: {', '.join(nonzero)}")
    return {"escapes": escapes, "escaped": escaped, "nonzero_exits": nonzero}


def mean_excluding(s, excluded):
    scores = [res.get("score") for _, res in results(s) if str(res.get("task_id")) not in excluded]
    return mean([x for x in scores if num(x)])


def view_names(res):
    out = []
    for v in res.get("skill_views") or []:
        name = v.get("name") if isinstance(v, dict) else v
        if isinstance(name, str):
            out.append(name)
    return out


per_task = {}
for arm, s in (("empty", empty), ("active", active)):
    for _, res in results(s):
        row = per_task.setdefault(str(res.get("task_id")), {"empty_scores": [], "active_scores": []})
        row[f"{arm}_scores"].append(res.get("score") if num(res.get("score")) else None)
per_task_rows = []
for tid in sorted(per_task):
    row = per_task[tid]
    e = mean([x for x in row["empty_scores"] if x is not None])
    a = mean([x for x in row["active_scores"] if x is not None])
    per_task_rows.append({"task_id": tid, **row, "empty_mean": e, "active_mean": a,
                          "delta": round(a - e, 6) if e is not None and a is not None else None})

evolved = set(active.get("skill_names") or [])
receipts = [{"rep": rep, "task_id": res.get("task_id"), "session": res.get("session"),
             "skills": sorted(set(view_names(res)) & evolved)}
            for rep, res in results(active) if set(view_names(res)) & evolved]
empty_views = sum(1 for _, res in results(empty) if set(view_names(res)) & evolved)


def cost(s):
    known = [res["usage"]["cost_usd"] for _, res in results(s)
             if isinstance(res.get("usage"), dict) and num(res["usage"].get("cost_usd"))]
    total = sum(1 for _ in results(s))
    return {"total_usd": round(sum(known), 6) if known else None, "runs": total, "runs_without_cost": total - len(known)}


flags = {"empty": arm_flags("empty", empty), "active": arm_flags("active", active)}
em, am = rep_means(empty), rep_means(active)
e_mean, a_mean = mean(em), mean(am)
delta = round(a_mean - e_mean, 6) if e_mean is not None and a_mean is not None else None
excluded = sorted({str(res.get("task_id")) for s in (empty, active) for _, res in results(s) if res.get("sandbox_escape")})
ex_e, ex_a = mean_excluding(empty, set(excluded)), mean_excluding(active, set(excluded))
delta_ex = round(ex_a - ex_e, 6) if ex_e is not None and ex_a is not None else None
met = bool(delta is not None and delta >= 0.15 and delta_ex is not None and delta_ex >= 0.15
           and len(em) >= 2 and len(am) >= 2 and not problems)
out = {
    # QA-F8: nothing written into the workspace names a path outside it -> held-out file by basename + sha256,
    # summaries relative to the workspace
    "ws": ws_dir, "tag_prefix": prefix, "tasks_file": os.path.basename(tasks_file), "tasks_sha256": tasks_sha,
    "summaries": {"empty": os.path.relpath(empty_path, ws_dir), "active": os.path.relpath(active_path, ws_dir)},
    "model": active.get("model"), "max_turns": active.get("max_turns"), "hermes_version": active.get("hermes_version"),
    "active_commit": active.get("active_commit"), "skill_names": sorted(evolved),
    "empty_means": em, "active_means": am, "empty_mean": e_mean, "active_mean": a_mean, "delta": delta,
    "per_task": per_task_rows,
    "skill_view_receipts": {"count": len(receipts), "items": receipts, "empty_arm_views_of_evolved": empty_views},
    "cost": {"empty": cost(empty), "active": cost(active)},
    "flags": flags,
    "excluded_tasks": excluded, "delta_excluding_flagged": delta_ex,
    "threshold": {"delta_min": 0.15, "repeats_min": 2, "met": met,
                  "rule": "delta >= 0.15 and delta_excluding_flagged >= 0.15, >= 2 repeats per arm, no problems"},
    "notes": notes,
    "problems": problems,
}
dest = os.path.join(ws_dir, "eval", f"{prefix}effectiveness.json")
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
    fh.write("\n")


def f(x):
    if x is None:
        return "—"
    if isinstance(x, float):
        text = f"{x:.4f}".rstrip("0").rstrip(".")
        return "0" if text in ("", "-0") else text
    return str(x)


def sd(x):
    return "—" if x is None else f"{x:+.4f}"


print(f"## Held-out effectiveness — `{os.path.basename(ws_dir)}` ({prefix or 'no prefix'})\n")
print(f"model `{out['model']}`, max_turns {out['max_turns']}, Hermes {out['hermes_version']}, "
      f"active commit `{out['active_commit']}`, skills {', '.join(out['skill_names']) or 'none'}\n")
print("| arm | " + " | ".join(f"rep {i + 1}" for i in range(max(len(em), len(am)))) + " | mean |")
print("|---|" + "---|" * (max(len(em), len(am)) + 1))
print("| empty | " + " | ".join(f(x) for x in em) + f" | {f(e_mean)} |")
print("| evolved | " + " | ".join(f(x) for x in am) + f" | {f(a_mean)} |")
print(f"\n**Δ (evolved − empty) = {sd(delta)}** · Δ excluding escape-flagged tasks = {sd(delta_ex)}"
      f"{' (' + ', '.join(excluded) + ')' if excluded else ''} · threshold met: {out['threshold']['met']} · "
      f"skill_view receipts of evolved skills: {len(receipts)} · cost empty {f(out['cost']['empty']['total_usd'])} "
      f"/ evolved {f(out['cost']['active']['total_usd'])} USD\n")
print("| task | empty scores | evolved scores | Δ |")
print("|---|---|---|---|")
for r in per_task_rows:
    print(f"| {r['task_id']} | {', '.join(f(x) for x in r['empty_scores'])} | "
          f"{', '.join(f(x) for x in r['active_scores'])} | {sd(r['delta'])} |")
for n in notes:
    print(f"\n> note: {n}")
for p in problems:
    print(f"\n> problem: {p}")
print(f"\nwritten: {dest}")
sys.exit(1 if problems else 0)
PY
