#!/usr/bin/env bash
# stock-compare.sh — the same bench on the STOCK upstream (PyPI wikiskill==0.1.5):
# fresh venv + workspace under ~/ops/hws-acceptance/stock-eff-<ts>/, our
# tasks.json (train/val) swapped in, `wikiskill evolve` <= 2 iterations wrapped
# whole in the team lock, then a JSON record: baseline, R_val per iteration,
# accept/reject, and whether any session's system prompt contained
# <available_skills> (D1). Upstream copies the live profile's config.yaml, .env
# and auth.json into <ws>/.hermes-home (D5); all three are deleted on exit.
# Upstream graders need plaintext answers, so its tasks.json is generated with
# `gen.py --plain` inside this stock run only: val answers sit in the stock
# workspace where its agents can read them (D19 on stock, recorded in the JSON).
# The plaintext test.json is deleted at once; nothing is written to
# ~/ops/hermes-wikiskill*.
#
#   stock-compare.sh [--iters 2] [--out FILE] [--base DIR]
set -Eeuo pipefail

BENCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SEALED_TASKS=$BENCH_DIR/tasks.json
ITERS=2
OUT=""
TS=$(date -u +%Y%m%dT%H%M%SZ)
BASE=${HWS_ACCEPTANCE_HOME:-$HOME/ops/hws-acceptance}/stock-eff-$TS
LOCK=${HWS_TEAM_LOCK:-$HOME/ops/.hws-team.lock}
PYTHON=${HWS_PYTHON:-python3}
DOMAIN=house-report
STOCK_SPEC=${HWS_STOCK_SPEC:-wikiskill==0.1.5}

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"; }
while (($#)); do
  case "$1" in
    --iters) ITERS=${2:?}; shift ;;
    --out) OUT=${2:?}; shift ;;
    --base) BASE=${2:?}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "stock-compare.sh: unknown argument $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ $ITERS =~ ^[12]$ ]] || { echo "stock-compare.sh: --iters must be 1 or 2" >&2; exit 2; }
[[ ! -e $BASE ]] || { echo "stock-compare.sh: $BASE already exists" >&2; exit 1; }
command -v flock >/dev/null || { echo "stock-compare.sh: flock not found" >&2; exit 1; }

WS=$BASE/ws
scrub() {  # D5: upstream copied secrets into the workspace profile; never leave them behind
  local f
  for f in .env auth.json config.yaml; do rm -f "$WS/.hermes-home/$f"; done
}
trap scrub EXIT
unset HERMES_HOME  # stock behaviour under test: bootstrap copies from ~/.hermes (read-only for it)

mkdir -p "$BASE"
echo "[stock] venv + $STOCK_SPEC in $BASE/venv"
"$PYTHON" -m venv "$BASE/venv"
# PyPI itself (not the user's mirror config), cache inside the run dir (never ~/.cache/pip)
PIP_CONFIG_FILE=/dev/null PIP_CACHE_DIR=$BASE/pipcache PIP_DISABLE_PIP_VERSION_CHECK=1 \
  "$BASE/venv/bin/python" -m pip install -q --index-url "${HWS_STOCK_INDEX:-https://pypi.org/simple}" "$STOCK_SPEC" \
  >"$BASE/pip.log" 2>&1
rm -rf "$BASE/pipcache"
STOCK=$BASE/venv/bin/wikiskill

echo "[stock] init $DOMAIN, then swap in our train/val tasks"
"$STOCK" init "$DOMAIN" --ws "$WS" >"$BASE/init.log" 2>&1
rm -rf "$WS/bench/tasks"
"$PYTHON" "$BENCH_DIR/gen.py" --plain "$BASE/plain" >/dev/null
mv "$BASE/plain/tasks.json" "$WS/tasks.json"
rm -rf "$BASE/plain"   # the plaintext test.json never stays anywhere
TASKS=$WS/tasks.json
# The deepseek route never reads auth.json; a copied OAuth refresh token could be ROTATED by a stock rollout,
# invalidating the owner's live one -> remove it before any rollout (qa credential hygiene).
rm -f "$WS/.hermes-home/auth.json"

echo "[stock] evolve --iters $ITERS under flock $LOCK (log: $BASE/evolve.log)"
rc=0
flock "$LOCK" "$STOCK" evolve "$DOMAIN" --ws "$WS" --iters "$ITERS" >"$BASE/evolve.log" 2>&1 || rc=$?
echo "[stock] evolve exit code $rc"
scrub

RESULT=$BASE/stock-compare.json
"$PYTHON" - "$WS" "$TASKS" "$rc" "$ITERS" "$STOCK_SPEC" "$BASE/evolve.log" "$RESULT" "$SEALED_TASKS" <<'PY'
import glob, hashlib, json, os, sys

ws, tasks_file, rc, iters, spec, log_path, dest, sealed_file = sys.argv[1:9]
state = {}
state_path = os.path.join(ws, "runs", "state.json")
if os.path.isfile(state_path):
    with open(state_path, encoding="utf-8") as fh:
        state = json.load(fh)

sessions = {}
for path in sorted(glob.glob(os.path.join(ws, "runs", "**", "session.jsonl"), recursive=True)
                   + glob.glob(os.path.join(ws, "raw", "traces", "**", "*.jsonl"), recursive=True)):
    tag = os.path.relpath(os.path.dirname(path), ws)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            sid = str(row.get("id") or row.get("session_id") or path)
            prompt = row.get("system_prompt") or ""
            cost = row.get("actual_cost_usd")
            if not isinstance(cost, (int, float)):
                cost = row.get("estimated_cost_usd")
            entry = sessions.setdefault(sid, {"tag": tag, "available_skills": "<available_skills>" in prompt,
                                              "cost_usd": cost if isinstance(cost, (int, float)) else None})
            entry["available_skills"] = entry["available_skills"] or "<available_skills>" in prompt

history = state.get("history") or []
costs = [s["cost_usd"] for s in sessions.values() if s["cost_usd"] is not None]
with open(tasks_file, "rb") as fh:
    tasks_sha = hashlib.sha256(fh.read()).hexdigest()
with open(sealed_file, "rb") as fh:
    sealed_sha = hashlib.sha256(fh.read()).hexdigest()
with open(log_path, encoding="utf-8", errors="replace") as fh:
    log_tail = [line.rstrip("\n") for line in fh.readlines()[-15:]]
out = {
    "target": "stock", "package": spec, "ws": ws, "iters_requested": int(iters), "evolve_exit_code": int(rc),
    "tasks_sha256": tasks_sha, "tasks_file": "gen.py --plain (upstream schema, plaintext answers)",
    "sealed_tasks_sha256": sealed_sha,
    "d19_plaintext_val_answers_in_workspace": True,
    "baseline": state.get("baseline"), "r_best": state.get("r_best"), "next_iter": state.get("next_iter"),
    "history": [{"iter": h.get("iter"), "r_val": h.get("r_val"), "accepted": h.get("accepted"),
                 "train_mean": h.get("train_mean"), "proposal": h.get("proposal")} for h in history],
    "accepted_count": sum(1 for h in history if h.get("accepted") is True),
    "sessions": {
        "exported": len(sessions),
        "with_available_skills": sum(1 for s in sessions.values() if s["available_skills"]),
        "tags_with_available_skills": sorted({s["tag"] for s in sessions.values() if s["available_skills"]}),
    },
    "cost_usd": round(sum(costs), 6) if costs else None,
    "sessions_without_cost": len(sessions) - len(costs),
    "secrets_scrubbed": [f for f in (".env", "auth.json", "config.yaml")
                         if not os.path.exists(os.path.join(ws, ".hermes-home", f))],
    "evolve_log_tail": log_tail,
}
with open(dest, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
    fh.write("\n")
print(json.dumps({k: out[k] for k in ("evolve_exit_code", "baseline", "r_best", "accepted_count", "sessions", "cost_usd")}))
PY
if [[ -n $OUT ]]; then cp "$RESULT" "$OUT"; fi
echo "[stock] written: $RESULT"
