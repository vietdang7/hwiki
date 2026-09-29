"""Dedicated Hermes profile management (`setup-profile`) — file-based creation.

Why files instead of `hermes profile create` (DECISIONS [core]): create would read the
ACTIVE profile's model block and copy its inline api_key (H4), write a `~/.local/bin`
alias unless --no-alias (H3), stage a dir under profiles/, and notify the live
gateway over its control socket (R1-qa facts 36, 40). A named profile is live when
its dir holds an identity marker such as config.yaml (fact 29), so writing the files
ourselves keeps every write inside ``~/.hermes/profiles/<name>/``.

Ownership (INTERFACE §6/§7): a profile is framework-owned only with ``.hws-profile.json``
(created_by hermes-wikiskill) bound to it: marker v2 records the profile name and canonical
path, so a copy (Hermes ``profile create --clone-all``) is foreign (R2-07). A legacy marker
without name/path counts only when the install's install-state.json lists the profile, and
a marker-less framework-only shell left by a crashed setup is repaired when listed (R2-06).
The marker is written FIRST (status incomplete) and completed last. Every other existing
profile — ``default``, otherprofile, … — is foreign: ``--i-am-owner`` installs ONLY
the operator skill into ``<profile>/skills/hermes-wikiskill`` (``default`` →
``~/.hermes/skills/hermes-wikiskill``), after a tar.gz backup of any existing copy and with a
receipt under ``$HWS_HOME/receipts``. config.yaml / .env / auth.json / SOUL.md / the marker of
a foreign profile are never touched (R1-06). ``--remove`` undoes those receipts (R1-07).
``$HWS_HOME`` defaults to the install prefix of the running venv (``<prefix>/venv`` next to
``<prefix>/install-state.json``), else ``~/ops/hermes-wikiskill`` (R2-08).
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import sys
import tarfile

from . import __version__, credentials, mini_yaml, resources
from .errors import HwsError, UsageError
from .util import atomic_write_json, atomic_write_text, now_iso, read_json, stamp, tree_digest

MARKER = ".hws-profile.json"
DEFAULT_PROFILE = "wikiskill"
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SUBDIRS = ("sessions", "skills", "memories", "logs")
SOUL = ("You are the hermes-wikiskill operator profile. Use the `hermes-wikiskill` skill to answer "
        "questions about WikiSkill workspaces on this host; report only what the tool prints.\n")


def hermes_root() -> str:
    return os.path.expanduser(os.environ.get("HWS_HERMES_ROOT") or "~/.hermes")


def check_name(name: str) -> str:
    if name != "default" and not _NAME.fullmatch(name or ""):
        raise UsageError(f"invalid profile name {name!r}")
    return name


def profile_dir(name: str) -> str:
    check_name(name)
    root = hermes_root()
    return root if name == "default" else os.path.join(root, "profiles", name)


def skills_dir(name: str) -> str:
    return os.path.join(profile_dir(name), "skills")


def _canon(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def hws_home() -> str:
    """$HWS_HOME, else the prefix of the install this code runs from, else ~/ops/hermes-wikiskill."""
    if os.environ.get("HWS_HOME"):
        return os.path.expanduser(os.environ["HWS_HOME"])
    prefix = os.path.dirname(os.path.abspath(sys.prefix))
    if os.path.basename(os.path.abspath(sys.prefix)) == "venv" and \
            os.path.isfile(os.path.join(prefix, "install-state.json")):
        return prefix
    return os.path.expanduser("~/ops/hermes-wikiskill")


def listed_profiles() -> dict[str, dict]:
    """{name: entry} of the install's install-state.json: v3 ``profiles[]``, plus the latest
    ``profile`` of a COMPLETED legacy install (kind ``legacy``; an incomplete state's
    ``profile`` is only what a failed run was asked for, never proof of ownership)."""
    st = read_json(os.path.join(hws_home(), "install-state.json"), {})
    if not isinstance(st, dict):
        return {}
    out = {}
    for e in st.get("profiles") if isinstance(st.get("profiles"), list) else []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            out[e["name"]] = {**e, "kind": str(e.get("kind") or "legacy")}
    if isinstance(st.get("profile"), str) and st.get("source_sha256"):
        out.setdefault(st["profile"], {"name": st["profile"], "kind": "legacy"})
    return out


def _listed_as_ours(name: str) -> bool:
    """A legacy marker counts: the state records the profile as ours (owned/legacy), or as
    `pending` for a dir that did not exist when install.sh listed it (QA-F21)."""
    e = listed_profiles().get(name) or {}
    return e.get("kind") in ("owned", "legacy") or (e.get("kind") == "pending" and e.get("absent_at_listing") is True)


def _listed_absent(name: str) -> bool:
    """R2-06 crash repair: only a dir that did NOT exist when install.sh listed it (QA-F21)."""
    return (listed_profiles().get(name) or {}).get("absent_at_listing") is True


def _is_shell(d: str) -> bool:
    """A marker-less dir holding only what setup_profile writes (a crashed pre-v2 setup)."""
    if os.path.islink(d) or not os.path.isdir(d):
        return False
    allowed = set(SUBDIRS) | {"config.yaml", ".env", "SOUL.md", ".no-bundled-skills"}
    for entry in os.listdir(d):
        if entry not in allowed:
            return False
    for sub in SUBDIRS:
        p = os.path.join(d, sub)
        if os.path.lexists(p) and (os.path.islink(p) or not os.path.isdir(p)):
            return False
        if os.path.isdir(p) and set(os.listdir(p)) - ({resources.OPERATOR_SKILL} if sub == "skills" else set()):
            return False
    for fname, text in (("SOUL.md", SOUL), (".no-bundled-skills", "hermes-wikiskill\n")):
        p = os.path.join(d, fname)
        if os.path.lexists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    if f.read() != text:
                        return False
            except (OSError, ValueError):
                return False
    return True


def ownership(name: str) -> tuple[str, str]:
    """(``owned``|``foreign``|``absent``, reason) — INTERFACE §7 marker v2 rules."""
    check_name(name)
    if name == "default":
        return "foreign", "the live default profile"
    d = profile_dir(name)
    if not os.path.lexists(d):
        return "absent", f"{d} does not exist"
    marker = read_json(os.path.join(d, MARKER), None)
    if isinstance(marker, dict) and marker.get("created_by") == "hermes-wikiskill":
        if "profile" in marker or "path" in marker:
            if marker.get("profile") == name and marker.get("path") == _canon(d):
                return "owned", "marker v2 matches"
            return "foreign", (f"{MARKER} belongs to profile {marker.get('profile')!r} at {marker.get('path')} "
                               "(a copy, e.g. `hermes profile create --clone-all`)")
        if _listed_as_ours(name):
            return "owned", "legacy marker, listed in install-state.json"
        return "foreign", f"legacy {MARKER} and install-state.json does not list this profile"
    if marker is None and not os.path.lexists(os.path.join(d, MARKER)) and _is_shell(d) and _listed_absent(name):
        return "owned", "framework-only shell of a crashed setup (absent when install.sh listed it)"
    return "foreign", f"no {MARKER} created by hermes-wikiskill"


def is_ours(name: str) -> bool:
    return ownership(name)[0] == "owned"


def is_foreign(name: str) -> bool:
    """An existing profile the framework did not create (always true for ``default``)."""
    return ownership(name)[0] == "foreign"


def is_live(name: str) -> bool:
    d = profile_dir(name)
    return os.path.isdir(d) and any(os.path.exists(os.path.join(d, m)) for m in (
        "config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db"))


def _source_home(source_profile: str | None) -> str:
    if os.environ.get("HWS_SOURCE_HOME"):
        return os.path.expanduser(os.environ["HWS_SOURCE_HOME"])
    return profile_dir(source_profile or "default")


def desired_config(plan: dict) -> dict:
    # no -t in operator use → Hermes enables ALL toolsets (incl. skills, terminal, file);
    # disabled_toolsets pinned empty so a copied/edited config cannot re-create D1 (D17).
    return {"model": plan["config"]["model"], "agent": {"disabled_toolsets": []}}


def _install_operator_skill(dst: str, dry_run: bool, actions: list[str]) -> bool:
    src = resources.operator_skill_dir()
    if not src:
        raise HwsError("operator skill hermes-wikiskill is not packaged with this install")
    if os.path.isdir(dst) and not os.path.islink(dst) and tree_digest(dst) == tree_digest(src):
        return False
    actions.append(f"install operator skill -> {dst}")
    if not dry_run:
        if os.path.islink(dst) or os.path.isfile(dst):
            os.remove(dst)
        elif os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return True


def _write_if_changed(path: str, text: str, mode: int, dry_run: bool, actions: list[str],
                      label: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            same = f.read() == text
    except OSError:
        same = False
    if same and (os.stat(path).st_mode & 0o777) == mode:
        return False
    actions.append(label)
    if not dry_run:
        atomic_write_text(path, text, mode=mode)
    return True


class ReceiptMismatchError(HwsError):
    """The operator skill changed since hermes-wikiskill installed it: nothing is undone."""

    exit_code = 4


def _hws_home() -> str:
    return hws_home()


def _dir_sha(path: str) -> str | None:
    return tree_digest(path) if os.path.isdir(path) and not os.path.islink(path) else None


def receipt_order(rid: str, kind: str, profile: str) -> tuple[str, int]:
    """Chronological key of a receipt id ``<stamp>-<kind>-<profile>[-k]`` (``-2`` sorts after the bare id)."""
    stamp_, _, rest = (rid or "").partition("-")
    tail = rest[len(f"{kind}-{profile}"):] if rest.startswith(f"{kind}-{profile}") else ""
    return stamp_, int(tail[1:]) if tail[1:].isdigit() else 1


def _setup_receipts(name: str) -> list[tuple[str, dict]]:
    out = []
    for p in glob.glob(os.path.join(_hws_home(), "receipts", f"*-setup-{name}*.json")):
        d = read_json(p)
        if isinstance(d, dict) and d.get("kind") == "operator-skill" and d.get("profile") == name:
            out.append((p, d))
    return sorted(out, key=lambda x: receipt_order(x[1].get("id") or "", "setup", name))


def _operator_skill_foreign(name: str, d: str, dry_run: bool) -> dict:
    """R1-06/R1-07: operator skill ONLY, after a backup of any existing copy, with a receipt."""
    dst = os.path.join(d, "skills", resources.OPERATOR_SKILL)
    actions: list[str] = []
    before = _dir_sha(dst)
    if os.path.lexists(dst) and before is None:
        raise HwsError(f"{dst} exists but is not a plain directory; refusing to replace it")
    src = resources.operator_skill_dir()
    if not src:
        raise HwsError("operator skill hermes-wikiskill is not packaged with this install")
    out = {"profile": name, "profile_dir": d, "changed": False, "actions": actions, "dry_run": dry_run,
           "operator_skill_only": True, "receipt": None}
    if before == tree_digest(src):
        return out
    ts = stamp()
    rid, k = f"{ts}-setup-{name}", 1
    while os.path.exists(os.path.join(_hws_home(), "receipts", rid + ".json")):
        k += 1
        rid = f"{ts}-setup-{name}-{k}"
    backup = os.path.join(_hws_home(), "backups", f"setup-{name}-{ts}{'' if k == 1 else f'-{k}'}.tar.gz") \
        if before else None
    if backup:
        actions.append(f"back up {dst} -> {backup}")
    actions.append(f"install operator skill -> {dst} (config.yaml/.env/auth.json untouched)")
    receipt_path = os.path.join(_hws_home(), "receipts", rid + ".json")
    out.update(changed=True, receipt=receipt_path)
    if dry_run:
        return out
    if backup:
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with tarfile.open(backup + ".tmp", "w:gz") as tar:
            tar.add(dst, arcname=resources.OPERATOR_SKILL, recursive=True)
        os.replace(backup + ".tmp", backup)
    # R3 m4: the receipt goes FIRST (incomplete), so a crash while the skill is written still
    # leaves a record that uninstall.sh finds (it reads every un-removed receipt), then completed
    created = []  # parent dirs this step creates (a fresh ~/.hermes has no skills/): removed again on undo
    parent = os.path.dirname(dst)
    while not os.path.lexists(parent) and parent != os.path.dirname(parent):
        created.append(parent)
        parent = os.path.dirname(parent)
    receipt = {"kind": "operator-skill", "id": rid, "ts": now_iso(), "profile": name, "path": dst,
               "backup": backup, "sha256_before": before, "sha256_target": tree_digest(src),
               "sha256_after": None, "status": "incomplete", "created_dirs": created}
    os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
    atomic_write_json(receipt_path, receipt)
    _install_operator_skill(dst, False, [])
    atomic_write_json(receipt_path, {**receipt, "status": "complete", "sha256_after": tree_digest(dst)})
    return out


def _restore_backup(backup: str, dst: str, want: str | None) -> None:
    """Extract an operator-skill tar.gz backup at ``dst`` with its recorded file/dir modes."""
    with tarfile.open(backup, "r:gz") as tar:
        members = tar.getmembers()
        for m in members:
            if m.name.startswith("/") or ".." in m.name.split("/") or \
                    m.name.split("/", 1)[0] != resources.OPERATOR_SKILL:
                raise HwsError(f"unsafe member in backup {backup}: {m.name}")
        if hasattr(tarfile, "tar_filter"):
            tar.extractall(os.path.dirname(dst), members=members, filter="tar")
        else:
            tar.extractall(os.path.dirname(dst), members=members)
    # the "tar" filter drops group/other write bits; put back what the backup recorded
    for m in sorted(members, key=lambda m: m.name.count("/"), reverse=True):
        p = os.path.join(os.path.dirname(dst), m.name)
        if (m.isfile() or m.isdir()) and not os.path.islink(p):
            os.chmod(p, m.mode & 0o777)
    if _dir_sha(dst) != want:
        raise HwsError(f"restored {dst} does not match the receipt's sha256_before")


def _backup_edited(name: str, dst: str) -> str:
    """Copy an owner-edited operator skill into a private 0700 dir under $HWS_HOME/backups."""
    root = os.path.join(_hws_home(), "backups")
    os.makedirs(root, exist_ok=True)
    base = os.path.join(root, f"edited-{name}-{stamp()}")
    path, k = base, 1
    while os.path.lexists(path):
        k += 1
        path = f"{base}-{k}"
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)
    shutil.copytree(dst, os.path.join(path, resources.OPERATOR_SKILL), symlinks=True)
    return path


def remove_operator_skill(name: str, *, i_am_owner: bool = False, dry_run: bool = False) -> dict:
    """Undo every un-removed operator-skill receipt of a foreign profile (R3-02: never stuck).

    The end state is what the OLDEST un-removed receipt found (``sha256_before``: its backup, or
    no dir). The skill dir decides the way there:
    - gone, or already that end state -> the chain counts as undone: receipts marked
      ``removed_at`` + ``removed_reason``, nothing else changes;
    - a content hermes-wikiskill knows (a receipt's installed/target digest, a backed-up
      earlier copy, or an edited copy already backed up) -> replaced by the end state;
    - anything else (the owner edited it) -> copied to a 0700 backup dir, recorded in the newest
      receipt, and ReceiptMismatchError (exit 4); re-running the same command then finishes.
    Exit 4 is also kept when the oldest receipt's backup is missing; removing the skill dir
    then unblocks. Nothing is changed before every check passed."""
    check_name(name)
    if not is_foreign(name):
        raise UsageError(f"profile {name!r} is framework-owned (or absent); --remove is only for the operator "
                         "skill installed into a foreign profile — uninstall.sh removes our own profile whole")
    if not i_am_owner:
        raise UsageError(f"--remove modifies the foreign profile {name!r}; the owner must add --i-am-owner")
    receipts = _setup_receipts(name)
    pending = [(p, r) for p, r in receipts if not r.get("removed_at")]
    out = {"profile": name, "changed": False, "actions": [], "receipt": None, "dry_run": dry_run,
           "receipts": [p for p, _ in pending]}
    if not pending:
        dst = os.path.join(profile_dir(name), "skills", resources.OPERATOR_SKILL)
        # R2-08/QA-F22: never a silent "done" while a skill dir of that name is there, unless it is
        # exactly what the first receipt found before hermes-wikiskill ever touched it (restored)
        restored = bool(receipts) and _dir_sha(dst) == receipts[0][1].get("sha256_before")
        if os.path.lexists(dst) and not restored:
            raise HwsError(f"{dst} exists but {os.path.join(_hws_home(), 'receipts')} holds no un-removed "
                           f"operator-skill receipt for profile {name!r}; nothing was changed. If it was "
                           "installed with a custom install.sh --prefix, re-run with HWS_HOME=<prefix>; "
                           "otherwise review it and remove it by hand")
        out["note"] = f"no operator-skill receipt for profile {name!r}: nothing to undo"
        return out
    oldest_p, oldest = pending[0]
    newest_p, newest = pending[-1]
    dst = str(oldest.get("path") or os.path.join(profile_dir(name), "skills", resources.OPERATOR_SKILL))
    if any(r.get("path") != dst for _, r in pending):
        raise HwsError(f"the operator-skill receipts of profile {name!r} name different paths; nothing was changed")
    rerun = (f"re-run the same command (uninstall.sh --yes, or HWS_HOME={_hws_home()} hws setup-profile "
             f"--profile {name} --remove --i-am-owner)")
    if os.path.lexists(dst) and _dir_sha(dst) is None:
        raise ReceiptMismatchError(f"{dst} is not a plain directory; nothing was changed. Remove it "
                                   f"(rm -rf {dst}), then {rerun}")
    target, cur = oldest.get("sha256_before"), _dir_sha(dst)
    out["receipt"] = newest_p
    if cur is None or cur == target:
        reason = ("the skill dir was already removed" if cur is None else
                  "the skill dir already holds what the oldest receipt found before hermes-wikiskill touched it")
        if cur is None and target is not None:
            reason += f" (your earlier copy stays in {oldest.get('backup')})"
        out["actions"].append(f"mark {len(pending)} receipt(s) undone: {reason}")
        out["note"] = f"nothing to restore: {reason}"
    else:
        known = {r.get(k) for _, r in pending for k in ("sha256_after", "sha256_target")}
        known |= {r.get("sha256_before") for _, r in pending if r.get("backup") and os.path.isfile(r["backup"])}
        known |= {r.get("edited_sha256") for _, r in pending
                  if r.get("edited_backup") and os.path.isdir(r["edited_backup"])}
        known.discard(None)
        if cur not in known:
            msg = (f"{dst} changed since it was installed by hermes-wikiskill (sha256 {cur[:12]} matches no "
                   f"receipt)")
            if dry_run:
                out["actions"] += [f"back up the edited {dst} to a 0700 dir under {os.path.join(_hws_home(), 'backups')}",
                                   "stop (exit 4); re-running the same command then restores/removes it"]
                out["edited"] = True
                out["note"] = msg
                return out
            saved = _backup_edited(name, dst)
            atomic_write_json(newest_p, {**newest, "edited_backup": saved, "edited_sha256": cur,
                                         "edited_at": now_iso()})
            raise ReceiptMismatchError(
                f"{msg}. Your edited copy is backed up to {saved} (recorded in {newest_p}); nothing else was "
                f"changed. To finish, {rerun}: it then restores/removes the skill per the receipts and the backup "
                "stays")
        if target is not None and not (oldest.get("backup") and os.path.isfile(oldest["backup"])):
            raise ReceiptMismatchError(
                f"backup {oldest.get('backup')} of receipt {os.path.basename(oldest_p)} is missing, so the copy of "
                f"{dst} that was there before hermes-wikiskill cannot be restored; nothing was changed. If you do "
                f"not need that copy, remove the skill dir (rm -rf {dst}), then {rerun}")
        out["actions"] += [f"undo receipt {os.path.basename(p)} (its copy is replaced below)"
                           for p, _ in reversed(pending[1:])]  # one action per receipt, newest first
        out["actions"].append(f"restore {dst} from {oldest['backup']}" if target is not None else f"remove {dst}")
        reason = "restored from the backup" if target is not None else "removed"
    if dry_run:
        return out
    if cur is not None and cur != target:
        shutil.rmtree(dst)
        if target is not None:
            _restore_backup(oldest["backup"], dst, target)
    if _dir_sha(dst) is None:
        for d in oldest.get("created_dirs") or []:  # innermost first, only while empty
            try:
                os.rmdir(d)
            except OSError:
                break
    ts = now_iso()
    for p, r in pending:
        atomic_write_json(p, {**r, "removed_at": ts, "removed_reason": reason})
    out["changed"] = True
    return out


def setup_profile(name: str = DEFAULT_PROFILE, *, source_profile: str | None = None,
                  i_am_owner: bool = False, dry_run: bool = False, remove: bool = False) -> dict:
    check_name(name)
    if remove:
        return remove_operator_skill(name, i_am_owner=i_am_owner, dry_run=dry_run)
    d = profile_dir(name)
    actions: list[str] = []
    kind, why = ownership(name)
    if kind == "foreign":
        if not i_am_owner:
            raise UsageError(f"profile {name!r} exists and was not created by hermes-wikiskill ({why}); "
                             "refusing to modify it. The owner may add --i-am-owner, which installs ONLY the "
                             "operator skill (config.yaml/.env/auth.json are never touched)")
        return _operator_skill_foreign(name, d, dry_run)
    if source_profile == name:
        raise UsageError("--source-profile must differ from --profile")
    plan = credentials.plan_profile(_source_home(source_profile))
    if not os.path.isdir(d):
        actions.append(f"create {d}")
    # R2-06: the marker goes FIRST (incomplete) so a crash leaves a profile we still own
    marker = os.path.join(d, MARKER)
    cur = read_marker(name)
    base = {"created_by": "hermes-wikiskill", "profile": name, "path": _canon(d),
            "version": cur.get("version") or __version__, "created": cur.get("created") or now_iso()}
    complete = cur.get("status") == "complete" and all(cur.get(k) == base[k] for k in ("created_by", "profile", "path"))
    if not complete:
        actions.append(f"write {MARKER} (v2, incomplete until the profile is complete)")
        if not dry_run:
            os.makedirs(d, exist_ok=True)
            atomic_write_json(marker, {**base, "status": "incomplete"})
    for sub in SUBDIRS:
        p = os.path.join(d, sub)
        if not os.path.isdir(p):
            if os.path.isdir(d):
                actions.append(f"mkdir {sub}/")
            if not dry_run:
                os.makedirs(p, exist_ok=True)
    _write_if_changed(os.path.join(d, "config.yaml"), mini_yaml.dumps(desired_config(plan)), 0o644,
                      dry_run, actions, "write config.yaml (key-less model block)")
    _write_if_changed(os.path.join(d, ".env"), credentials.render_env(plan["env"]), 0o600, dry_run,
                      actions, f"write .env ({', '.join(plan['env_names']) or 'no key found'}, 0600)")
    if credentials.auth_json_issues(d, plan["provider"]):  # Hermes' fingerprint-only pool is kept
        actions.append("remove auth.json (foreign provider / secret fields)")
        if not dry_run:
            os.remove(os.path.join(d, "auth.json"))
    for fname, text in (("SOUL.md", SOUL), (".no-bundled-skills", "hermes-wikiskill\n")):
        if not os.path.exists(os.path.join(d, fname)):
            _write_if_changed(os.path.join(d, fname), text, 0o644, dry_run, actions, f"write {fname}")
    _install_operator_skill(os.path.join(d, "skills", resources.OPERATOR_SKILL), dry_run, actions)
    if not complete and not dry_run:
        atomic_write_json(marker, {**base, "status": "complete"})
    return {"profile": name, "profile_dir": d, "changed": bool(actions), "actions": actions,
            "dry_run": dry_run, "provider": plan["provider"], "env_names": plan["env_names"],
            "missing_credentials": plan["missing"]}


def read_marker(name: str) -> dict:
    d = read_json(os.path.join(profile_dir(name), MARKER), {})
    return d if isinstance(d, dict) else {}
