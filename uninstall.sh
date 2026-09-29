#!/usr/bin/env bash
# hermes-wikiskill uninstaller. Run it ON the Hermes host.
#
# Only what install.sh created is removed, and only under a PREFIX that holds
# its install-state.json. Every profile ANY install run set up is handled
# (install-state.json v3 `profiles[]` + the latest `profile`, R2-02), plus every
# profile that still has an un-removed operator-skill receipt under
# $PREFIX/receipts:
# - framework-owned (marker v2 .hws-profile.json bound to that name and path; a
#   legacy marker or a framework-only shell only when install-state lists it):
#   backup -> rm -rf -> wait and verify it stays gone;
# - foreign (`default`, any profile we did not create, or a --clone-all copy of
#   ours): NEVER deleted; its operator skill installed with --i-am-owner is
#   undone from its receipts (`hws setup-profile --profile P --remove
#   --i-am-owner`: backup restored or dir deleted) before the venv goes. Every
#   undo is verified with --dry-run first, so a changed skill stops the
#   uninstall before anything is removed.
# Then our symlinks, venv and install files are removed. Workspaces, receipts
# and diagnostics/ (operator-only files naming the readable out-of-workspace
# paths of voided role runs, T10g) are kept unless --purge; receipts and
# diagnostics are always copied into the backup, workspaces are tar.gz-backed-up
# first on --purge. Backups are always kept.
#
# Never runs hermes (no `hermes profile delete`: it leaves a tombstone and asks
# for a gateway restart), never touches gateways or ~/.local/bin entries that
# do not point into our venv. PREFIX is canonicalised and refused when it is /,
# $HOME (or above it), or overlaps ~/.hermes or ~/ops/wikiskill.
set -Eeuo pipefail

PREFIX=${HWS_PREFIX:-$HOME/ops/hermes-wikiskill}
PROFILE=""
BIN_DIR=${HWS_BIN_DIR:-$HOME/.local/bin}
HERMES_ROOT=${HWS_HERMES_ROOT:-$HOME/.hermes}
BACKUP_ROOT=${HWS_BACKUP_ROOT:-$HOME/ops/hermes-wikiskill-backups}
PYTHON=${HWS_PYTHON:-python3}
WAIT_S=${HWS_UNINSTALL_WAIT:-90}   # >= 90 s on the host (dashboard ticker, 60 s period); tests shorten it
YES=0
DRY_RUN=0
PURGE=0

usage() {
  cat <<'EOF'
usage: uninstall.sh [--yes] [--prefix DIR] [--profile NAME] [--purge] [--dry-run]

  --yes           do not ask for confirmation (required when stdin is not a terminal)
  --prefix DIR    install root (default ~/ops/hermes-wikiskill); must hold install-state.json
  --profile NAME  optional check: must be a profile this install set up (install-state.json).
                  Every profile the install set up is handled, whatever --profile says:
                  our own profiles are removed; a profile we did not create (incl. `default`)
                  is never removed, only the operator skill installed into it is undone.
  --purge         also delete workspaces/, receipts/ and diagnostics/ under the prefix (all backed up first)
  --dry-run       print the plan; write nothing
EOF
}

while (($#)); do
  case "$1" in
    --yes|-y) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --purge) PURGE=1 ;;
    --prefix) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PREFIX=$2; shift ;;
    --prefix=*) PREFIX=${1#*=} ;;
    --profile) [[ $# -ge 2 ]] || { usage >&2; exit 2; }; PROFILE=$2; shift ;;
    --profile=*) PROFILE=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "uninstall.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }
trap 'die "unexpected failure at line $LINENO (exit $?)"' ERR

valid_name() { [[ $1 == default || $1 =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; }
[[ -z $PROFILE ]] || valid_name "$PROFILE" || die "invalid profile name: $PROFILE"
[[ $WAIT_S =~ ^[0-9]+$ ]] || die "HWS_UNINSTALL_WAIT must be a number of seconds"

# R1-05/R1-22: canonical PREFIX + protected paths (same rule as install.sh)
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
) || { echo "uninstall.sh: unsafe --prefix (see above)" >&2; exit 2; }
VENV=$PREFIX/venv
STATE=$PREFIX/install-state.json
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=$BACKUP_ROOT/$TS

echo "hermes-wikiskill uninstall: prefix $PREFIX"
if [[ ! -f $STATE ]]; then
  for p in "$VENV" "$PREFIX/workspaces"; do
    [[ ! -e $p ]] || die "refusing: $STATE is missing, so $PREFIX was not installed by install.sh (found $p)"
  done
  echo "nothing to uninstall: no $STATE"
  exit 0
fi

# ------------------------------------------------------------------ plan: one line per profile
# "<mode>\t<name>\t<dir>\t<reason>"; mode = owned | undo | foreign | absent.
# owned  = our marker v2 bound to this name + canonical path; a legacy marker (no name/path) or a
#          marker-less framework-only shell only when install-state lists the profile as ours;
# undo   = not ours, with an un-removed operator-skill receipt (the CLI undoes the receipt chain;
#          an operator-skill entry without one is kept untouched: nothing of ours to undo, R3 m7);
# foreign/absent = kept, nothing to do.
# parent = a parent dir install.sh created for an owned profile (a fresh ~/.hermes/profiles),
#          removed again when it is empty afterwards.
PLAN=$("$PYTHON" - "$STATE" "$PREFIX" "$HERMES_ROOT" <<'PYEOF'
import glob, json, os, re, sys
state_path, prefix, hroot = sys.argv[1:4]
NAME = re.compile(r"(default|[a-z0-9][a-z0-9_-]{0,63})")
SUBDIRS = ("sessions", "skills", "memories", "logs")
SOUL = ("You are the hermes-wikiskill operator profile. Use the `hermes-wikiskill` skill to answer "
        "questions about WikiSkill workspaces on this host; report only what the tool prints.\n")
canon = lambda x: os.path.realpath(os.path.abspath(os.path.expanduser(x)))


def load(p):
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


st = load(state_path)
if not isinstance(st, dict):
    sys.exit(f"cannot read {state_path}")
listed, absent, parents = {}, {}, {}
for e in st.get("profiles") if isinstance(st.get("profiles"), list) else []:
    if isinstance(e, dict) and isinstance(e.get("name"), str):
        listed[e["name"]] = str(e.get("kind") or "legacy")
        absent[e["name"]] = e.get("absent_at_listing") is True
        if isinstance(e.get("created_parent"), str) and e["created_parent"] == os.path.join(hroot, "profiles"):
            parents[e["name"]] = e["created_parent"]
if isinstance(st.get("profile"), str):
    if st.get("source_sha256"):  # the latest profile of a COMPLETED legacy install only
        listed.setdefault(st["profile"], "legacy")
if not isinstance(st.get("profiles"), list) and st.get("source_sha256") and "wikiskill" not in listed:
    # R3 m5: a completed pre-v3 install whose last run was the owner step lists only `default`;
    # its own default-named profile is known by its legacy marker (a clone cannot be named wikiskill)
    m = load(os.path.join(hroot, "profiles", "wikiskill", ".hws-profile.json"))
    if isinstance(m, dict) and m.get("created_by") == "hermes-wikiskill" and "profile" not in m and "path" not in m:
        listed["wikiskill"] = "legacy"
pending = []
paths = glob.glob(os.path.join(prefix, "receipts", "*.json"))
for e in st.get("profiles") if isinstance(st.get("profiles"), list) else []:
    if isinstance(e, dict):
        paths += [r for r in e.get("receipts") or [] if isinstance(r, str)]
for p in sorted(set(paths)):
    r = load(p)
    if isinstance(r, dict) and r.get("kind") == "operator-skill" and not r.get("removed_at") \
            and isinstance(r.get("profile"), str) and r["profile"] not in pending:
        pending.append(r["profile"])
names = list(listed) + [n for n in pending if n not in listed]


def shell(d):
    if os.path.islink(d) or not os.path.isdir(d):
        return False
    if set(os.listdir(d)) - set(SUBDIRS) - {"config.yaml", ".env", "SOUL.md", ".no-bundled-skills"}:
        return False
    for sub in SUBDIRS:
        p = os.path.join(d, sub)
        if os.path.lexists(p) and (os.path.islink(p) or not os.path.isdir(p)):
            return False
        if os.path.isdir(p) and set(os.listdir(p)) - ({"hermes-wikiskill"} if sub == "skills" else set()):
            return False
    for fname, text in (("SOUL.md", SOUL), (".no-bundled-skills", "hermes-wikiskill\n")):
        p = os.path.join(d, fname)
        if os.path.lexists(p):
            try:
                with open(p) as fh:
                    if fh.read() != text:
                        return False
            except (OSError, ValueError):
                return False
    return True


for name in names:
    if not NAME.fullmatch(name):
        sys.exit(f"install-state.json or a receipt names an invalid profile: {name!r}")
    d = hroot if name == "default" else os.path.join(hroot, "profiles", name)
    # a legacy marker counts when the state records the profile as ours, or as pending for a dir
    # that did not exist when install.sh listed it; a marker-less shell only in that last case (QA-F21)
    ours_listed = listed.get(name) in ("owned", "legacy") or (listed.get(name) == "pending" and absent.get(name, False))
    if not os.path.lexists(d):
        print("absent", name, d, "not present", sep="\t")
        continue
    mode, why = "foreign", "not created by hermes-wikiskill"
    m = load(os.path.join(d, ".hws-profile.json"))
    if name == "default":
        why = "the live default profile"
    elif isinstance(m, dict) and m.get("created_by") == "hermes-wikiskill":
        if "profile" in m or "path" in m:
            if m.get("profile") == name and m.get("path") == canon(d):
                mode, why = "owned", "marker v2"
            else:
                why = "its marker belongs to another profile (a copy)"
        elif ours_listed:
            mode, why = "owned", "legacy marker, listed in install-state.json"
    elif not os.path.lexists(os.path.join(d, ".hws-profile.json")) and absent.get(name, False) and shell(d):
        mode, why = "owned", "framework-only shell of a crashed setup"
    if mode == "foreign" and name in pending:
        mode = "undo"
    elif mode == "foreign" and listed.get(name) == "operator-skill":
        why += "; no un-removed operator-skill receipt, so nothing of ours to undo"
    print(mode, name, d, why, sep="\t")
    if mode == "owned" and name in parents:
        print("parent", name, parents[name], "created by install.sh", sep="\t")
PYEOF
) || die "cannot plan the uninstall from $STATE"

OWNED=(); OWNED_DIRS=(); UNDO=(); KNOWN=(); PARENTS=()
while IFS=$'\t' read -r mode name dir why; do
  [[ -n ${mode:-} ]] || continue
  case $mode in
    owned) OWNED+=("$name"); OWNED_DIRS+=("$dir") ;;
    undo) UNDO+=("$name") ;;
    parent) PARENTS+=("$dir"); continue ;;
  esac
  KNOWN+=("$name")
done <<<"$PLAN"
if [[ -n $PROFILE ]] && [[ " ${KNOWN[*]} " != *" $PROFILE "* ]]; then
  die "refusing --profile $PROFILE: this install did not set up profile '$PROFILE' (install-state.json lists: ${KNOWN[*]:-none}); other profiles are never touched"
fi
[[ ${#KNOWN[@]} -gt 0 ]] || echo "install-state.json lists no profile"

ours() {  # ours NAME -> true if BIN_DIR/NAME is a symlink into our venv
  local link=$BIN_DIR/$1
  [[ -L $link ]] && [[ $(readlink "$link") == "$VENV/"* ]]
}

removals=()
for name in hermes-wikiskill hws; do
  if ours "$name"; then removals+=("$BIN_DIR/$name"); fi
done
for p in "$VENV" "$PREFIX/install.log" "$STATE"; do
  if [[ -e $p || -L $p ]]; then removals+=("$p"); fi
done
if ((PURGE)); then
  for p in "$PREFIX/workspaces" "$PREFIX/receipts" "$PREFIX/diagnostics"; do
    if [[ -e $p ]]; then removals+=("$p"); fi
  done
fi

while IFS=$'\t' read -r mode name dir why; do
  [[ -n ${mode:-} ]] || continue
  case $mode in
    owned) echo "profile: $name (owned) - $why -> removed" ;;
    undo) echo "profile: $name (foreign) - $why -> kept; its operator skill (if any) is undone from its receipts" ;;
    foreign) echo "profile: $name (foreign) - $why -> kept, not touched" ;;
    absent) echo "profile: $name (absent) -> nothing to do" ;;
    parent) echo "  $dir was created by install.sh -> removed if empty afterwards" ;;
  esac
done <<<"$PLAN"

if ((DRY_RUN)); then
  echo "dry-run plan (nothing written):"
  echo "  would back up to $BACKUP (receipts, diagnostics, install.log, install-state.json$( ((${#OWNED[@]})) && printf ', profile archives' )$( ((PURGE)) && [[ -d $PREFIX/workspaces ]] && printf ', workspaces archive' ))"
  if ((${#UNDO[@]})); then for n in "${UNDO[@]}"; do
    echo "  would NOT remove profile $n (not created by hermes-wikiskill); would undo its operator skill: hws setup-profile --profile $n --remove --i-am-owner"
  done; fi
  if ((${#OWNED_DIRS[@]})); then for d in "${OWNED_DIRS[@]}"; do
    echo "  would remove profile dir $d, wait ${WAIT_S}s, verify absent"
  done; fi
  if ((${#removals[@]})); then for p in "${removals[@]}"; do echo "  would remove $p"; done; fi
  exit 0
fi

if ((!YES)); then
  [[ -t 0 ]] || { echo "uninstall.sh: not a terminal; re-run with --yes" >&2; exit 2; }
  read -r -p "Remove hermes-wikiskill (prefix $PREFIX; remove profiles: ${OWNED[*]:-none}; undo operator skill in: ${UNDO[*]:-none})? [y/N] " answer
  [[ $answer == [yY]* ]] || { echo "aborted"; exit 1; }
fi

# ------------------------------------------------------------------ foreign profiles: undo the operator skill first (needs our venv)
if ((${#UNDO[@]})); then
  HWS=$VENV/bin/hermes-wikiskill
  [[ -x $HWS ]] || die "cannot undo the operator skill of profile(s) ${UNDO[*]}: $HWS is missing (nothing removed)"
  undo_run() {  # undo_run NAME [--dry-run] -> sets undo_rc / undo_out
    undo_rc=0
    undo_out=$(trap - ERR; HWS_HOME=$PREFIX HWS_HERMES_ROOT=$HERMES_ROOT "$HWS" setup-profile --profile "$1" --remove --i-am-owner "${@:2}" --json 2>&1) || undo_rc=$?
    case $undo_rc in
      0) ;;
      4) die "the operator skill in profile $1 changed since it was installed, or its backup is missing; nothing else was removed. Follow the step below, then re-run: bash $0 --yes$( ((PURGE)) && printf ' --purge' ). ($undo_out)" ;;
      *) die "hws setup-profile --remove failed for profile $1 (exit $undo_rc); nothing removed: $undo_out" ;;
    esac
  }
  for n in "${UNDO[@]}"; do undo_run "$n" --dry-run; done  # verify every receipt chain before changing anything
  for n in "${UNDO[@]}"; do
    undo_run "$n"
    echo "operator skill of profile $n: $undo_out"
    echo "profile $n kept (not created by hermes-wikiskill)"
  done
fi

# ------------------------------------------------------------------ backup (0700: the profile .env holds a key)
(umask 077 && mkdir -p "$BACKUP")
backed=()
if ((${#OWNED[@]})); then for n in "${OWNED[@]}"; do
  (umask 077 && tar -czf "$BACKUP/profile-$n.tgz" -C "$HERMES_ROOT/profiles" "$n")
  backed+=("profile-$n.tgz")
done; fi
for p in receipts diagnostics install.log install-state.json; do
  if [[ -e $PREFIX/$p ]]; then cp -pR "$PREFIX/$p" "$BACKUP/$p"; backed+=("$p"); fi
done
if ((PURGE)) && [[ -d $PREFIX/workspaces ]]; then
  (umask 077 && tar -czf "$BACKUP/workspaces.tgz" -C "$PREFIX" workspaces)
  backed+=("workspaces.tgz")
  echo "workspaces backed up: $BACKUP/workspaces.tgz"
fi
echo "backup: $BACKUP (${backed[*]:-empty})"

# ------------------------------------------------------------------ remove
if ((${#OWNED_DIRS[@]})); then
  for d in "${OWNED_DIRS[@]}"; do rm -rf "$d"; echo "removed profile dir $d"; done
  echo "waiting ${WAIT_S}s to verify the removed profile dir(s) stay absent"
  sleep "$WAIT_S"
  for d in "${OWNED_DIRS[@]}"; do
    [[ ! -e $d ]] || die "$d reappeared after ${WAIT_S}s; something is still writing it (check gateways/dashboard) - not retrying blindly"
    echo "verified: $d absent after ${WAIT_S}s"
  done
fi
if ((${#PARENTS[@]})); then for d in "${PARENTS[@]}"; do
  if [[ -d $d && ! -L $d ]] && [[ -z $(ls -A "$d") ]]; then rmdir "$d"; echo "removed empty $d (created by install.sh)"; fi
done; fi

if ((${#removals[@]})); then
  for p in "${removals[@]}"; do rm -rf "$p"; echo "removed $p"; done
fi

if [[ -d $PREFIX ]] && [[ -z $(ls -A "$PREFIX") ]]; then rmdir "$PREFIX"; echo "removed empty $PREFIX"; fi

# ------------------------------------------------------------------ report what is kept
echo "kept:"
echo "  $BACKUP_ROOT (backups, incl. $BACKUP)"
if [[ -d $PREFIX ]]; then
  while IFS= read -r entry; do echo "  $PREFIX/$entry"; done < <(ls -A "$PREFIX")
fi
echo "uninstall complete"
