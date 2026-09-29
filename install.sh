#!/usr/bin/env bash
# hermes-wikiskill installer. Run it ON the Hermes host from the synced source
# directory (e.g. bash ~/ops/hermes-wikiskill-src/install.sh --yes).
#
# preflight -> venv -> pip install (non-editable, this directory) -> symlinks
# -> dedicated Hermes profile (hws setup-profile) -> hws doctor -> summary.
# Idempotent: a second run with the same source changes nothing and says so.
#
# Host rules honoured here: never `hermes --version` (it writes ~/.hermes);
# every hermes call gets a scratch HERMES_HOME under /tmp/hws-*; nothing under
# ~/.hermes is written except through `hws setup-profile` (our own profile, or
# ONLY the operator skill of an owner-approved existing profile); ~/.local/bin
# entries we do not own are never touched. PREFIX is canonicalised and refused
# when it is /, $HOME (or above it), or overlaps ~/.hermes or ~/ops/wikiskill; an
# existing $PREFIX/venv is used only with the install-state.json we wrote.
#
# install-state.json (v3, R2-02) keeps `profiles: [{name, kind, path, receipts}]` for
# EVERY profile any run of this installer set up (merged, never overwritten by the
# latest --profile; `profile` = latest, for compat). A profile whose dir does not
# exist yet is recorded as `pending` (absent_at_listing: true, never flipped back)
# BEFORE setup-profile runs, so a crashed run is still uninstallable; an existing dir
# is recorded only after setup-profile accepted it (ours, or --i-am-owner = operator
# skill only), so a hand-made profile is never adopted (QA-F21). An unreadable
# install-state.json is refused before anything is written (QA-F23).
set -Eeuo pipefail
umask 022

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
PREFIX=${HWS_PREFIX:-$HOME/ops/hermes-wikiskill}
PROFILE=wikiskill
SOURCE_PROFILE=default
BIN_DIR=${HWS_BIN_DIR:-$HOME/.local/bin}
PYTHON=${HWS_PYTHON:-python3}
YES=0
DRY_RUN=0
OWNER=0
MIN_DISK_MB=${HWS_MIN_DISK_MB:-500}
REQUIRED_FLAGS=(--toolsets)
MODERN_FLAGS=(--query-file --oneshot --run-budget --in --max-turns)

usage() {
  cat <<'EOF'
usage: install.sh [--yes] [--prefix DIR] [--profile NAME] [--source-profile NAME] [--dry-run]

  --yes                  do not ask for confirmation (required when stdin is not a terminal)
  --prefix DIR           install root (default ~/ops/hermes-wikiskill): venv, install.log, workspaces
  --profile NAME         dedicated Hermes profile to create/refresh (default wikiskill)
  --source-profile NAME  profile whose provider key is copied, minimal (default default)
  --dry-run              run the preflight and print the plan; write nothing
  --i-am-owner           owner approval for an EXISTING profile hermes-wikiskill did not create
                         (required for `default`): installs ONLY the operator skill into its
                         skills/ (backup + receipt); its config.yaml/.env/auth.json are never
                         touched. Undo: uninstall.sh, or
                         hws setup-profile --profile NAME --remove --i-am-owner
EOF
}

while (($#)); do
  case "$1" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --i-am-owner) OWNER=1 ;;
    --prefix) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PREFIX=$2; shift ;;
    --prefix=*) PREFIX=${1#*=} ;;
    --profile) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PROFILE=$2; shift ;;
    --profile=*) PROFILE=${1#*=} ;;
    --source-profile) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; SOURCE_PROFILE=$2; shift ;;
    --source-profile=*) SOURCE_PROFILE=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

HERMES_ROOT=${HWS_HERMES_ROOT:-$HOME/.hermes}
# R1-05/R1-22: canonical PREFIX (absolute, symlinks resolved, no trailing slash) + protected paths
PREFIX=$("$PYTHON" - "$PREFIX" "$HOME" "$HERMES_ROOT" <<'PYEOF'
import os, sys
canon = lambda x: os.path.realpath(os.path.abspath(os.path.expanduser(x)))
p, home, hroot = (canon(x) for x in sys.argv[1:4])
inside = lambda a, b: a == b or a.startswith(b.rstrip("/") + "/")
if p == "/" or inside(home, p):
    sys.exit(f"refusing prefix {p}: it is / or $HOME, or contains $HOME")
for prot in sorted({hroot, canon(os.path.join(home, ".hermes")), canon(os.path.join(home, "ops", "wikiskill"))}):
    if inside(p, prot) or inside(prot, p):
        sys.exit(f"refusing prefix {p}: it overlaps the protected {prot}")
print(p)
PYEOF
) || { echo "install.sh: unsafe --prefix (see above)" >&2; exit 2; }
VENV=$PREFIX/venv
LOG=$PREFIX/install.log
STATE=$PREFIX/install-state.json
SCRATCH=""
PIP_TMP=""
CHANGES=()

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() {
  printf '%s\n' "$*"
  if ((!DRY_RUN)) && [[ -f ${STATE:-} ]]; then printf '%s [install] %s\n' "$(ts)" "$*" >>"$LOG"; fi
}
die() {
  printf 'install.sh: %s\n' "$*" >&2
  if ((!DRY_RUN)) && [[ -f ${STATE:-} ]]; then printf '%s [install] ERROR: %s\n' "$(ts)" "$*" >>"$LOG"; fi
  exit 1
}
cleanup() {
  [[ -n $SCRATCH && -d $SCRATCH ]] && rm -rf "$SCRATCH"
  [[ -n $PIP_TMP && -d $PIP_TMP ]] && rm -rf "$PIP_TMP"
  return 0
}
trap cleanup EXIT
trap 'die "unexpected failure at line $LINENO (exit $?)"' ERR

# QA-F23: never silently rewrite a corrupt state (it lists every profile to uninstall); checked
# before the first log line, so a refused run writes nothing at all
if [[ -e $STATE || -L $STATE ]] && ! "$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d, dict) else 1)' "$STATE" 2>/dev/null; then
  printf 'install.sh: %s exists but is not a valid JSON object; refusing (nothing changed). Restore it from %s or review it by hand.\n' \
    "$STATE" "${HWS_BACKUP_ROOT:-$HOME/ops/hermes-wikiskill-backups}/<ts>/install-state.json" >&2
  exit 1
fi

valid_name() { [[ $1 =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; }
valid_name "$PROFILE" || die "invalid profile name: $PROFILE"
valid_name "$SOURCE_PROFILE" || die "invalid source profile name: $SOURCE_PROFILE"
if [[ $PROFILE == default ]] && ((!OWNER)); then
  die "--profile default targets the live profile; the owner must add --i-am-owner"
fi

# ------------------------------------------------------------------ preflight
log "hermes-wikiskill install: source $SRC_DIR -> prefix $PREFIX (profile $PROFILE)"

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
  || die "python >= 3.10 required ($PYTHON)"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
  || die "python venv/ensurepip missing for $PYTHON (Ubuntu: sudo apt install python3-venv)"
PY_VERSION=$("$PYTHON" -c 'import platform; print(platform.python_version())')

HERMES=${HERMES_BIN:-$(command -v hermes || true)}
[[ -n $HERMES && -x $HERMES ]] || die "hermes not found on PATH (set HERMES_BIN)"

# Hermes version from its package metadata, via the python its launcher uses.
hermes_python() {
  local real first
  real=$(readlink -f "$HERMES" 2>/dev/null || printf '%s' "$HERMES")
  if [[ -n ${HERMES_PYTHON:-} ]]; then printf '%s' "$HERMES_PYTHON"; return; fi
  first=$(head -n1 "$real" 2>/dev/null || true)
  if [[ $first == '#!'*python* ]]; then
    first=${first#\#!}; first=${first%% *}
    [[ -x $first ]] && { printf '%s' "$first"; return; }
  fi
  local c
  for c in "$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/hermes-agent/.venv/bin/python"; do
    [[ -x $c ]] && { printf '%s' "$c"; return; }
  done
}
HPY=$(hermes_python || true)
HERMES_VERSION=unknown
if [[ -n $HPY ]]; then
  HERMES_VERSION=$("$HPY" -I -B -c 'import importlib.metadata as m; print(m.version("hermes-agent"))' 2>/dev/null || echo unknown)
fi
[[ $HERMES_VERSION == unknown ]] && log "warn: Hermes version unknown (no hermes-agent metadata found); doctor re-checks"
log "hermes: $HERMES (version $HERMES_VERSION); python: $PYTHON $PY_VERSION"

SCRATCH=$(mktemp -d /tmp/hws-install-probe.XXXXXX)
with_timeout() {  # coreutils timeout when present (Linux hosts); plain run otherwise
  local secs=$1; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi
}
HELP=$(HERMES_HOME=$SCRATCH with_timeout 120 "$HERMES" chat --help 2>&1 || true)
missing=()
for f in "${REQUIRED_FLAGS[@]}"; do grep -q -- "$f" <<<"$HELP" || missing+=("$f"); done
((${#missing[@]} == 0)) || die "hermes chat lacks required flag(s): ${missing[*]}"
legacy=()
for f in "${MODERN_FLAGS[@]}"; do grep -q -- "$f" <<<"$HELP" || legacy+=("$f"); done
if ((${#legacy[@]})); then
  log "warn: hermes chat lacks ${legacy[*]} -> the framework uses its legacy argv fallback"
else
  log "hermes chat flags: ${REQUIRED_FLAGS[*]} ${MODERN_FLAGS[*]} present"
fi
rm -rf "$SCRATCH"; SCRATCH=""

probe=$PREFIX
while [[ ! -d $probe ]]; do probe=$(dirname "$probe"); done
avail_mb=$(( $(df -Pk "$probe" | awk 'NR==2 {print $4}') / 1024 ))
((avail_mb >= MIN_DISK_MB)) || die "only ${avail_mb} MB free under $probe (need ${MIN_DISK_MB} MB)"
log "disk: ${avail_mb} MB free under $probe"
if [[ -r /proc/meminfo ]]; then
  mem_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  if ((mem_mb < 512)); then log "warn: only ${mem_mb} MB RAM available; keep one rollout at a time"
  else log "ram: ${mem_mb} MB available"; fi
fi

# Source identity: version + content hash (build outputs and caches excluded).
VERSION=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' "$SRC_DIR/pyproject.toml" | head -n1)
[[ -n $VERSION ]] || die "cannot read version from $SRC_DIR/pyproject.toml"
SRC_SHA=$("$PYTHON" - "$SRC_DIR" <<'PY'
import hashlib, os, sys
root = sys.argv[1]
skip_dirs = {".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist", ".git"}
h = hashlib.sha256()
for d, dirs, files in os.walk(root):
    dirs[:] = sorted(x for x in dirs if x not in skip_dirs and not x.endswith(".egg-info"))
    for f in sorted(files):
        if f.endswith((".pyc", ".pyo")):
            continue
        p = os.path.join(d, f)
        h.update(os.path.relpath(p, root).encode() + b"\0")
        with open(p, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
print(h.hexdigest())
PY
)
log "source: hermes-wikiskill $VERSION sha256 ${SRC_SHA:0:16}"

state_update() {  # state_update init SRC | pending NAME DIR OWNER | profile NAME KIND PATH RECEIPT | unpend NAME DIR | final SHA VERSION PROFILE SRC
  HWS_STATE_HERMES_ROOT=$HERMES_ROOT "$PYTHON" - "$STATE" "$@" <<'STATEPY'
import json, os, sys
path, mode, args = sys.argv[1], sys.argv[2], sys.argv[3:]
try:
    with open(path) as fh:
        st = json.load(fh)
except FileNotFoundError:
    st = {}
except (OSError, ValueError) as e:
    sys.exit(f"{path} is not valid JSON ({e}); refusing to rewrite it")
if not isinstance(st, dict):
    sys.exit(f"{path} is not a JSON object; refusing to rewrite it")
profs = st.get("profiles")
profs = [p for p in profs if isinstance(p, dict) and isinstance(p.get("name"), str)] if isinstance(profs, list) else []
if isinstance(st.get("profile"), str) and st.get("source_sha256") and st["profile"] not in [p["name"] for p in profs]:
    profs.append({"name": st["profile"], "kind": "legacy", "path": None, "receipts": []})  # completed v1/v2 install
if not isinstance(st.get("profiles"), list) and st.get("source_sha256") and "wikiskill" not in [p["name"] for p in profs]:
    # R3 m5: a completed pre-v3 install whose LAST run was the owner step recorded `profile: default`,
    # so its own default-named profile is listed only by its legacy marker (a clone cannot be named wikiskill)
    try:
        with open(os.path.join(os.environ["HWS_STATE_HERMES_ROOT"], "profiles", "wikiskill", ".hws-profile.json")) as fh:
            m = json.load(fh)
    except (OSError, ValueError):
        m = None
    if isinstance(m, dict) and m.get("created_by") == "hermes-wikiskill" and "profile" not in m and "path" not in m:
        profs.append({"name": "wikiskill", "kind": "legacy", "path": None, "receipts": []})


def entry(name):
    for p in profs:
        if p["name"] == name:
            return p
    profs.append({"name": name, "kind": "pending", "path": None, "receipts": []})
    return profs[-1]


if mode == "init":
    (src,) = args
    if not st.get("source_sha256"):
        st.update(source_dir=src, status="incomplete")
elif mode == "pending":  # a dir that does not exist yet (or, with --i-am-owner, one that does)
    name, pdir, owner = args
    parent = os.path.dirname(pdir)
    if not os.path.lexists(pdir):
        e = entry(name)
        e["absent_at_listing"] = True  # true is never flipped back by a later re-list
        if not os.path.lexists(parent) and name != "default":  # a fresh ~/.hermes has no profiles/ yet
            e["created_parent"] = parent
    elif owner == "1" and name not in [p["name"] for p in profs]:
        # R3 m4: listed before the owner step touches its skill; dropped again when no receipt results
        profs.append({"name": name, "kind": "operator-skill", "path": pdir, "receipts": []})
elif mode == "unpend":  # R3 m6: setup-profile failed; forget an entry that recorded nothing
    name, pdir = args
    profs = [p for p in profs if not (p["name"] == name and not p.get("receipts") and (
        (p.get("kind") == "pending" and not os.path.lexists(pdir)) or p.get("kind") == "operator-skill"))]
elif mode == "profile":
    name, kind, ppath, receipt = args
    e = entry(name)
    e["kind"] = kind
    e["path"] = ppath or e.get("path")
    rs = e["receipts"] = [r for r in e.get("receipts") or [] if isinstance(r, str)]
    if receipt and receipt not in rs:
        rs.append(receipt)
    if kind == "operator-skill" and not rs:  # R3 m7: the skill was already identical, nothing of ours to undo
        profs = [p for p in profs if p is not e]
elif mode == "final":
    sha, version, profile, src = args
    st = {"schema": 3, "source_sha256": sha, "version": version, "profile": profile, "source_dir": src}
st["profiles"] = profs
new = json.dumps(st, sort_keys=True) + "\n"
try:
    with open(path) as fh:
        old = fh.read()
except OSError:
    old = None
if new != old:
    with open(path + ".tmp", "w") as fh:
        fh.write(new)
    os.replace(path + ".tmp", path)
    print("changed")
STATEPY
}

state_field() {  # state_field KEY -> value from install-state.json ("" if absent)
  [[ -f $STATE ]] || return 0
  "$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$STATE" "$1" 2>/dev/null || true
}

if [[ -e $VENV && ! -f $STATE ]]; then
  die "refusing $VENV: it exists but $STATE does not (not created by this installer); choose another --prefix"
fi

NEED_VENV=0; NEED_PIP=0
if [[ ! -x $VENV/bin/python ]] || ! "$VENV/bin/python" -c 'import sys' 2>/dev/null; then NEED_VENV=1; NEED_PIP=1; fi
if ((!NEED_PIP)); then
  if [[ $(state_field source_sha256) != "$SRC_SHA" || $(state_field version) != "$VERSION" ]] \
     || [[ ! -x $VENV/bin/hermes-wikiskill || ! -x $VENV/bin/hws ]] \
     || ! "$VENV/bin/python" -I -c 'import hermes_wikiskill' 2>/dev/null; then
    NEED_PIP=1
  fi
fi

link_state() {  # link_state NAME -> ok | create | replace | foreign
  local link=$BIN_DIR/$1 target=$VENV/bin/$1 cur
  if [[ -L $link ]]; then
    cur=$(readlink "$link")
    if [[ $cur == "$target" ]]; then echo ok
    elif [[ $cur == "$VENV/"* ]]; then echo replace
    else echo foreign; fi
  elif [[ -e $link ]]; then echo foreign
  else echo create; fi
}
for name in hermes-wikiskill hws; do
  [[ $(link_state "$name") == foreign ]] \
    && die "refusing to overwrite $BIN_DIR/$name: it exists and does not point into $VENV"
done

if ((DRY_RUN)); then
  log "dry-run plan (nothing written):"
  ((NEED_VENV)) && log "  would create venv $VENV with $PYTHON"
  ((NEED_PIP)) && log "  would pip install $SRC_DIR (hermes-wikiskill $VERSION) into $VENV"
  for name in hermes-wikiskill hws; do
    s=$(link_state "$name"); [[ $s == ok ]] || log "  would $s symlink $BIN_DIR/$name -> $VENV/bin/$name"
  done
  log "  would run: hermes-wikiskill setup-profile --profile $PROFILE --source-profile $SOURCE_PROFILE --json$( ((OWNER)) && printf ' --i-am-owner' )"
  log "  would run: hermes-wikiskill doctor --json --profile $PROFILE"
  exit 0
fi

if ((!YES)); then
  [[ -t 0 ]] || { echo "install.sh: not a terminal; re-run with --yes" >&2; exit 2; }
  read -r -p "Install hermes-wikiskill $VERSION into $PREFIX and set up profile '$PROFILE'? [y/N] " answer
  [[ $answer == [yY]* ]] || { echo "aborted"; exit 1; }
fi

mkdir -p "$PREFIX"
# ownership record first: a failed run can be re-run and uninstalled (R1-05)
state_update init "$SRC_DIR" >/dev/null || die "cannot write $STATE"
log "---- install run $(ts) (hermes-wikiskill $VERSION, hermes $HERMES_VERSION)"

# ------------------------------------------------------------------ venv + package
# R6: pip must not write ~/.cache/pip -> run-local cache removed by the EXIT trap.
# The user's pip.conf (index/mirror) is kept.
PIP_TMP=/tmp/hws-pip-$$
rm -rf "$PIP_TMP" && mkdir -m 700 "$PIP_TMP"
export PIP_CACHE_DIR=$PIP_TMP PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
log "pip cache: $PIP_TMP (removed at exit)"
if ((NEED_VENV)); then
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV" >>"$LOG" 2>&1 || die "venv creation failed (see $LOG)"
  CHANGES+=("venv")
  log "created venv $VENV"
fi
if ((NEED_PIP)); then
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input -q \
      --force-reinstall --no-deps "$SRC_DIR" >>"$LOG" 2>&1 || die "pip install failed (see $LOG)"
  "$VENV/bin/python" -I -c 'import hermes_wikiskill' || die "installed package does not import"
  CHANGES+=("package")
  log "installed hermes-wikiskill $VERSION into $VENV"
else
  log "package: hermes-wikiskill $VERSION already installed from this source"
fi

# ------------------------------------------------------------------ symlinks
mkdir -p "$BIN_DIR"
for name in hermes-wikiskill hws; do
  case $(link_state "$name") in
    ok) ;;
    create|replace) ln -sfn "$VENV/bin/$name" "$BIN_DIR/$name"; CHANGES+=("symlink:$name"); log "linked $BIN_DIR/$name" ;;
    *) die "refusing to overwrite $BIN_DIR/$name" ;;
  esac
done
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) log "note: $BIN_DIR is not on PATH; use $BIN_DIR/hws or add it" ;; esac

# ------------------------------------------------------------------ profile + doctor
export HWS_HOME=$PREFIX HERMES_BIN=$HERMES
HWS=$VENV/bin/hermes-wikiskill
json_get() { "$VENV/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get(sys.argv[1])))' "$1"; }

if [[ $PROFILE == default ]]; then PROFILE_DIR=$HERMES_ROOT; else PROFILE_DIR=$HERMES_ROOT/profiles/$PROFILE; fi
# R2-02: a profile about to be CREATED is listed before anything is written to it (crash-safe);
# an existing dir is listed only after setup-profile accepted it (QA-F21)
state_update pending "$PROFILE" "$PROFILE_DIR" "$OWNER" >/dev/null || die "cannot write $STATE"
setup_args=(setup-profile --profile "$PROFILE" --source-profile "$SOURCE_PROFILE" --json)
if ((OWNER)); then setup_args+=(--i-am-owner); fi
setup_rc=0
setup_out=$(trap - ERR; "$HWS" "${setup_args[@]}" 2>>"$LOG") || setup_rc=$?
if ((setup_rc)); then
  state_update unpend "$PROFILE" "$PROFILE_DIR" >/dev/null || true
  die "setup-profile failed for profile $PROFILE (see $LOG)"
fi
changed=$(json_get changed <<<"$setup_out" 2>/dev/null || echo null)
case $changed in
  true) CHANGES+=("profile:$PROFILE"); log "profile $PROFILE created/refreshed" ;;
  false) log "profile $PROFILE already up to date" ;;
  *) die "setup-profile returned no 'changed' field" ;;
esac
setup_fields=$("$VENV/bin/python" -c '
import json, sys
d = json.load(sys.stdin)
print("operator-skill" if d.get("operator_skill_only") else "owned")
print(d.get("profile_dir") or "")
print(d.get("receipt") or "")
' <<<"$setup_out") || die "setup-profile output is not JSON"
p_dir=""; p_receipt=""
{ read -r p_kind; read -r p_dir || true; read -r p_receipt || true; } <<<"$setup_fields"  # $(...) drops empty trailing lines
state_update profile "$PROFILE" "$p_kind" "$p_dir" "$p_receipt" >/dev/null || die "cannot write $STATE"
if [[ -n $p_receipt ]]; then
  log "operator-skill receipt: $p_receipt (undo: uninstall.sh, or HWS_HOME=$PREFIX hws setup-profile --profile $PROFILE --remove --i-am-owner)"
fi

doctor_rc=0
doctor_out=$("$HWS" doctor --json --profile "$PROFILE" 2>>"$LOG") || doctor_rc=$?
doctor_lines=$("$VENV/bin/python" -c '
import json, sys
d = json.load(sys.stdin)
for c in d.get("checks", []):
    print("%-4s %s: %s" % (str(c.get("status", "?")).upper(), c.get("id", "?"), c.get("detail", "")))
print("OK" if d.get("ok") is True else "NOT-OK")
' <<<"$doctor_out" 2>/dev/null) || die "doctor output is not JSON (exit $doctor_rc)"
while IFS= read -r line; do [[ $line == OK || $line == NOT-OK ]] || log "doctor: $line"; done <<<"$doctor_lines"
[[ $(tail -n1 <<<"$doctor_lines") == OK && $doctor_rc -eq 0 ]] || die "doctor reported failing checks (exit $doctor_rc)"

# ------------------------------------------------------------------ state + summary
state_changed=$(state_update final "$SRC_SHA" "$VERSION" "$PROFILE" "$SRC_DIR") || die "cannot write $STATE"
if [[ -n $state_changed ]] && ((${#CHANGES[@]} == 0)); then CHANGES+=("state"); fi

if ((${#CHANGES[@]} == 0)); then
  log "no changes: hermes-wikiskill $VERSION already installed, profile $PROFILE up to date, doctor ok"
else
  log "changes: ${CHANGES[*]}"
  log "installed: hermes-wikiskill $VERSION at $VENV; commands $BIN_DIR/hermes-wikiskill, $BIN_DIR/hws; profile $PROFILE; doctor ok"
fi
log "next: hws doctor --profile $PROFILE | hws init <ws> --tasks <bench>/tasks.json | hermes -p $PROFILE chat"
