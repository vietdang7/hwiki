#!/usr/bin/env bash
# run-effectiveness.sh <ws> — the R4 experiment on the house-report bench:
#   init <ws> with tasks.json (train/val only) -> evolve baseline (iters 0) ->
#   abort before ANY test eval if the val baseline >= 0.5 (bench too easy) ->
#   evolve one iteration at a time, at most --max-iters, stopping early after an
#   accept with R_val >= 0.8 -> freeze (active commit + sha256 of tasks/test) ->
#   measure.sh (held-out test.json, empty vs evolved, --repeat N each).
#
#   run-effectiveness.sh <ws> [--max-iters 4] [--repeat 2] [--max-turns 15]
#                             [--tag-prefix P] [--ws-root DIR] [--measure-without-accept]
#                             [--no-measure]   (stop after freeze; run measure.sh yourself)
#
# Exit: 0 measured · 1 failure · 2 usage · 3 bench too easy (no test eval ran)
#       4 no skill accepted (not measured unless --measure-without-accept)
# Every live rollout is serialized through HWS_ROLLOUT_LOCK (team lock file).
set -Eeuo pipefail

BENCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
TASKS=$BENCH_DIR/tasks.json
TEST=$BENCH_DIR/test.json
MAX_ITERS=4
REPEAT=2
MAX_TURNS=15
PREFIX_TAG=""
WS_ROOT=${HWS_WS_ROOT:-${HWS_HOME:-$HOME/ops/hermes-wikiskill}/workspaces}
MEASURE_ANYWAY=0
NO_MEASURE=0
BASELINE_MAX=0.5
ACCEPT_STOP=0.8
WS=""

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --max-iters) MAX_ITERS=${2:?}; shift ;;
    --repeat) REPEAT=${2:?}; shift ;;
    --max-turns) MAX_TURNS=${2:?}; shift ;;
    --tag-prefix) PREFIX_TAG=${2?}; shift ;;
    --ws-root) WS_ROOT=${2:?}; shift ;;
    --measure-without-accept) MEASURE_ANYWAY=1 ;;
    --no-measure) NO_MEASURE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "run-effectiveness.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    *) [[ -z $WS ]] || { echo "run-effectiveness.sh: one workspace only" >&2; exit 2; }; WS=$1 ;;
  esac
  shift
done
[[ -n $WS ]] || { usage >&2; exit 2; }
[[ $WS =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "run-effectiveness.sh: bad workspace name" >&2; exit 2; }
[[ $MAX_ITERS =~ ^[1-4]$ ]] || { echo "run-effectiveness.sh: --max-iters must be 1..4 (SPEC R4)" >&2; exit 2; }

die() { echo "run-effectiveness.sh: $*" >&2; exit 1; }
say() { echo "[effectiveness] $*"; }
HWS=${HWS_CLI:-$(command -v hws || echo "$HOME/.local/bin/hws")}
PY=${HWS_PY:-python3}
[[ -x $HWS ]] || die "hws not found (set HWS_CLI)"
export HWS_ROLLOUT_LOCK=${HWS_ROLLOUT_LOCK:-$HOME/ops/.hws-team.lock}
WS_DIR=$WS_ROOT/$WS
STATE=$WS_DIR/runs/state.json
hws() { "$HWS" --ws-root "$WS_ROOT" "$@"; }
sha() { "$PY" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
state() {  # state EXPR -> python expression over s (state.json) printed as JSON
  "$PY" -c 'import json,sys; s=json.load(open(sys.argv[1])); print(json.dumps(eval(sys.argv[2])))' "$STATE" "$1"
}

# ------------------------------------------------------------------ init
if [[ -f $WS_DIR/tasks.json ]]; then
  [[ $(sha "$WS_DIR/tasks.json") == $(sha "$TASKS") ]] \
    || die "$WS_DIR/tasks.json differs from $TASKS; use a fresh workspace name"
  say "resuming workspace $WS_DIR"
else
  say "init $WS with $TASKS (train/val only; test.json stays outside the workspace)"
  hws init "$WS" --tasks "$TASKS"
fi
[[ ! -e $WS_DIR/test.json ]] || die "a test.json is inside the workspace; refusing (held-out contamination)"

# ------------------------------------------------------------------ baseline + bench calibration
if [[ ! -f $STATE ]] || [[ $(state 's.get("baseline")') == null ]]; then
  say "baseline: validation split with no skills (evolve --iters 0)"
  hws evolve "$WS" --iters 0 --max-turns "$MAX_TURNS"
fi
BASELINE=$(state 's["baseline"]')
say "val baseline = $BASELINE"
if "$PY" -c 'import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)' "$BASELINE" "$BASELINE_MAX"; then
  say "bench too easy for this model: baseline $BASELINE >= $BASELINE_MAX; aborting BEFORE any test eval"
  exit 3
fi

# ------------------------------------------------------------------ evolve, one iteration at a time
for ((k = 1; k <= MAX_ITERS; k++)); do
  next=$(state 's.get("next_iter", 1)')
  if ((next > k)); then continue; fi
  last_accept=$(state '[h for h in s.get("history", []) if h.get("accepted") is True][-1:]')
  if "$PY" -c 'import json,sys; h=json.loads(sys.argv[1]); sys.exit(0 if h and h[0].get("r_val", 0) >= float(sys.argv[2]) else 1)' \
      "$last_accept" "$ACCEPT_STOP"; then
    say "stop: an accepted iteration already reached R_val >= $ACCEPT_STOP"
    break
  fi
  say "evolve iteration $k/$MAX_ITERS"
  hws evolve "$WS" --iters "$k" --max-turns "$MAX_TURNS"
  say "after iter $k: $(state '{"r_best": s.get("r_best"), "next_iter": s.get("next_iter"), "last": (s.get("history") or [None])[-1]}')"
done
say "evolve done: $(state '{"baseline": s.get("baseline"), "r_best": s.get("r_best"), "history": [(h.get("iter"), h.get("r_val"), h.get("accepted")) for h in s.get("history", [])]}')"

ACCEPTED=$(state 'sum(1 for h in s.get("history", []) if h.get("accepted") is True)')
if ((ACCEPTED == 0)) && ((!MEASURE_ANYWAY)); then
  say "no proposal was accepted by the gate; not measuring (diagnose: skills visible? proposer output? bench?)"
  exit 4
fi

# ------------------------------------------------------------------ freeze
ACTIVE_COMMIT=$(git -C "$WS_DIR/skills/active" rev-parse HEAD 2>/dev/null || echo unknown)
ACTIVE_SKILLS=$(cd "$WS_DIR/skills/active" 2>/dev/null && for d in */; do [[ -f $d/SKILL.md ]] && printf '%s ' "${d%/}"; done; true)
mkdir -p "$WS_DIR/eval"
FREEZE=$WS_DIR/eval/${PREFIX_TAG}freeze.json
"$PY" -c 'import json,sys; json.dump(dict(zip(sys.argv[2::2], sys.argv[3::2])), open(sys.argv[1], "w"), indent=2, sort_keys=True)' \
  "$FREEZE" active_commit "$ACTIVE_COMMIT" active_skills "${ACTIVE_SKILLS% }" \
  tasks_sha256 "$(sha "$TASKS")" test_sha256 "$(sha "$TEST")" frozen_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
say "frozen: commit $ACTIVE_COMMIT, skills [${ACTIVE_SKILLS% }], tasks $(sha "$TASKS" | cut -c1-16), test $(sha "$TEST" | cut -c1-16) -> $FREEZE"

# ------------------------------------------------------------------ held-out measurement
if ((NO_MEASURE)); then
  say "stopped after freeze (--no-measure); next: bash $BENCH_DIR/measure.sh $WS --repeat $REPEAT --max-turns $MAX_TURNS${PREFIX_TAG:+ --tag-prefix $PREFIX_TAG}"
  exit 0
fi
exec bash "$BENCH_DIR/measure.sh" "$WS" --repeat "$REPEAT" --tag-prefix "$PREFIX_TAG" \
  --tasks "$TEST" --max-turns "$MAX_TURNS" --ws-root "$WS_ROOT"
