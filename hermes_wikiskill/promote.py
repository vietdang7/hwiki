"""`promote` / `demote` (D9): the reviewed path from a gate-ACCEPTED skill to a Hermes profile.

promote: copies skills exactly as committed at the LAST accepted gate commit of the
workspace (never uncommitted candidates) into ``<profile>/skills/``; before that it
writes a tar.gz backup + sha256 manifest of the skills dir under ``$HWS_HOME/backups``
and a receipt under ``$HWS_HOME/receipts``. Refuses name collisions unless ``--replace``,
and any profile hermes-wikiskill did not create (``default`` and every other live profile
without ``.hws-profile.json``, except ``hws-test-*``) unless ``--i-am-owner`` (R1-21; owner
policy: adopt needs review + approval). Skills containing symlinks are refused (R1-16).
Undo: ``hws demote --profile <P> --last [--i-am-owner]``.

demote: removes the promoted skills and restores replaced ones from the backup. In
``.usage.json`` (Hermes' per-skill counters, keyed by skill name — tools/skill_usage.py)
only the promoted names' entries are removed/restored; other skills' counters bumped
since the promote are kept (QA-F5). Then the skills dir is verified against the
pre-promote manifest (``identical``; Hermes' own new ``*.lock`` files are ignored) —
other entries are never touched.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tarfile

from . import compat, gating, gitutil, profiles
from .errors import HermesIncompatibleError, HwsError, UsageError
from .util import (atomic_write_json, canonical_sha256, check_skill_name, now_iso, read_json,
                   sha256_bytes, sha256_file, stamp)

USAGE_FILE = ".usage.json"


def hws_home() -> str:
    return profiles.hws_home()  # R2-08: same receipts/backups root as setup-profile


def receipts_dir() -> str:
    return os.path.join(hws_home(), "receipts")


def backups_dir() -> str:
    return os.path.join(hws_home(), "backups")


def manifest(root: str) -> dict[str, str]:
    """relpath -> 'F <sha256>' | 'L <target>' | 'D' for everything under root."""
    out: dict[str, str] = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for n in list(dirnames):
            p = os.path.join(dirpath, n)
            rel = os.path.normpath(os.path.join(rel_dir, n))
            if os.path.islink(p):
                out[rel] = f"L {os.readlink(p)}"
                dirnames.remove(n)
            else:
                out[rel] = "D"
        for n in filenames:
            p = os.path.join(dirpath, n)
            rel = os.path.normpath(os.path.join(rel_dir, n))
            out[rel] = f"L {os.readlink(p)}" if os.path.islink(p) else f"F {sha256_file(p)}"
    return dict(sorted(out.items()))


def accepted_gate(ws: str) -> dict:
    """The last gate-ACCEPTED iteration of a workspace and its skills/active commit."""
    state = gating.load_state(ws)
    acc = [h for h in state.get("history", []) if h.get("accepted") is True]
    if not acc:
        raise HwsError(f"workspace {ws} has no gate-ACCEPTED iteration; nothing to promote")
    h = max(acc, key=lambda x: x.get("iter", 0))
    commit = h.get("active_commit")
    if not commit:  # older state: find the accept commit by message
        log = gating.git_active(ws, "log", "--format=%H %s").stdout.splitlines()
        commit = next((ln.split()[0] for ln in log if f" accept iter-{h['iter']} " in f"{ln} "), None)
        if not commit:
            raise HwsError(f"accept commit of iter {h['iter']} not found in skills/active history")
    return {"iter": h["iter"], "r_val": h.get("r_val"), "r_best": state.get("r_best"),
            "commit": commit}


def committed_skills(ws: str, commit: str) -> dict[str, dict[str, bytes]]:
    """{skill: {relpath: bytes}} of every skill (dir with SKILL.md) in ``commit``."""
    d = gating.active_dir(ws)
    files = gitutil.git(d, "ls-tree", "-r", "--name-only", commit).stdout.splitlines()
    out: dict[str, dict[str, bytes]] = {}
    for path in files:
        if "/" not in path:
            continue
        name, rel = path.split("/", 1)
        out.setdefault(name, {})[rel] = b""
    for name in list(out):
        if "SKILL.md" not in out[name]:
            del out[name]
            continue
        for rel in list(out[name]):
            p = subprocess.run(["git", "show", f"{commit}:{name}/{rel}"], cwd=d,
                               capture_output=True, stdin=subprocess.DEVNULL)
            if p.returncode != 0:
                raise HwsError(f"git show {name}/{rel} failed")
            out[name][rel] = p.stdout
    return out


def committed_symlinks(ws: str, commit: str) -> list[str]:
    """Paths committed as symlinks (git mode 120000) at ``commit`` of skills/active."""
    lines = gitutil.git(gating.active_dir(ws), "ls-tree", "-r", commit).stdout.splitlines()
    return sorted(line.split("\t", 1)[1] for line in lines if line.startswith("120000 "))


def _skill_sha(files: dict[str, bytes]) -> str:
    return canonical_sha256({rel: sha256_bytes(b) for rel, b in sorted(files.items())})


def _backup(skills: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with tarfile.open(dest + ".tmp", "w:gz") as tar:
        if os.path.isdir(skills):
            for n in sorted(os.listdir(skills)):
                tar.add(os.path.join(skills, n), arcname=n, recursive=True)
    os.replace(dest + ".tmp", dest)


def _hermes_version() -> str | None:
    try:
        return compat.hermes_version(compat.resolve_binary(None))
    except HermesIncompatibleError:
        return None


def needs_owner(profile: str) -> bool:
    """R1-21: every profile the framework did not create, except the qa/test ``hws-test-*`` ones."""
    return profile == "default" or (not profile.startswith("hws-test-") and not profiles.is_ours(profile))


def _require_owner(profile: str, i_am_owner: bool, verb: str, dry_run: bool) -> None:
    """A --dry-run plan writes nothing, so the owner can review it before approving."""
    if needs_owner(profile) and not i_am_owner and not dry_run:
        raise UsageError(f"refusing to {verb} the profile {profile!r} without --i-am-owner: hermes-wikiskill did "
                         "not create it (owner policy: adopting skills needs owner review + approval)")


def promote(ws: str, profile: str, *, skills: list[str] | None = None, replace: bool = False,
            i_am_owner: bool = False, dry_run: bool = False) -> dict:
    profiles.check_name(profile)
    _require_owner(profile, i_am_owner, "promote into", dry_run)
    if not profiles.is_live(profile):
        raise HwsError(f"profile {profile!r} does not exist at {profiles.profile_dir(profile)}")
    gate = accepted_gate(ws)
    available = committed_skills(ws, gate["commit"])
    names = sorted(available) if not skills else [check_skill_name(s) for s in skills]
    missing = [n for n in names if n not in available]
    if missing:
        raise HwsError(f"not gate-accepted in {ws} (commit {gate['commit'][:12]}): {missing}")
    if not names:
        raise HwsError("the accepted skill set is empty")
    linked = [p for p in committed_symlinks(ws, gate["commit"]) if p.split("/", 1)[0] in names]
    if linked:  # R1-16: a symlink could pull e.g. ~/.hermes/.env into the profile
        raise HwsError(f"refusing to promote skills containing symlinks: {linked}")
    target = profiles.skills_dir(profile)
    collisions = [n for n in names if os.path.lexists(os.path.join(target, n))]
    if collisions and not replace:
        raise HwsError(f"name collision in {target}: {collisions} (review, then pass --replace)")
    ts = stamp()
    rid = f"{ts}-promote-{profile}"
    k = 1
    while os.path.exists(os.path.join(receipts_dir(), rid + ".json")):
        k += 1
        rid = f"{ts}-promote-{profile}-{k}"
    backup = os.path.join(backups_dir(), f"{rid}-skills.tar.gz")
    receipt = {"kind": "promote", "id": rid, "ts": now_iso(), "source_ws": os.path.abspath(ws),
               "profile": profile, "skills_dir": target,
               "skills": [{"name": n, "sha256": _skill_sha(available[n]), "replaced": n in collisions}
                          for n in names],
               "gate": {"iter": gate["iter"], "r_val": gate["r_val"], "r_best": gate["r_best"],
                        "commit": gate["commit"]},
               "backup": backup, "hermes_version": _hermes_version()}
    if dry_run:
        return {**receipt, "dry_run": True}
    before = manifest(target)
    _backup(target, backup)
    atomic_write_json(backup + ".manifest.json", before)
    receipt["manifest_before_sha256"] = canonical_sha256(before)
    os.makedirs(target, exist_ok=True)
    for n in names:
        dst = os.path.join(target, n)
        if os.path.islink(dst) or os.path.isfile(dst):
            os.remove(dst)
        elif os.path.isdir(dst):
            shutil.rmtree(dst)
        for rel, data in available[n].items():
            p = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
    atomic_write_json(os.path.join(receipts_dir(), rid + ".json"), receipt)
    return {**receipt, "dry_run": False}


def merge_usage(path: str, backup: bytes | None, names: list[str]) -> dict:
    """QA-F5: undo a promote in ``.usage.json`` for ``names`` only.

    Each promoted name gets its pre-promote entry back (or none); every other entry stays
    as Hermes left it. When the result equals the backup, the backup BYTES are written
    (byte-identical demote, DoD 9b); an unparseable current file is left untouched."""
    try:
        before = json.loads(backup) if backup is not None else {}
    except ValueError:
        before = None
    exists = os.path.isfile(path)
    try:
        with open(path, encoding="utf-8") as f:
            current = json.load(f) if exists else {}
    except (OSError, ValueError):
        current = None
    if not isinstance(current, dict) or not isinstance(before, dict):
        return {"action": "left", "reason": "unparseable .usage.json (current or backup)"}
    merged = {k: v for k, v in current.items() if k not in names}
    restored = [n for n in names if n in before]
    for n in restored:
        merged[n] = before[n]
    removed = sorted(n for n in names if n in current and n not in before)
    if merged == before and backup is not None:
        with open(path + ".tmp", "wb") as f:
            f.write(backup)
        os.replace(path + ".tmp", path)
        action = "restored"
    elif not merged and backup is None:
        if exists:
            os.remove(path)
        action = "removed"
    else:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        os.replace(path + ".tmp", path)
        action = "merged"
    return {"action": action, "removed": removed, "restored": restored,
            "kept": sorted(k for k in merged if k not in names)}


def load_receipt(rid: str) -> dict:
    p = os.path.join(receipts_dir(), os.path.basename(rid).removesuffix(".json") + ".json")
    d = read_json(p)
    if not isinstance(d, dict):
        raise HwsError(f"receipt not found: {p}")
    return d


def _receipts(kind: str) -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(receipts_dir(), f"*-{kind}-*.json"))):
        d = read_json(p)
        if isinstance(d, dict) and d.get("kind") == kind:
            out.append(d)
    return sorted(out, key=lambda d: profiles.receipt_order(d.get("id") or "", kind, str(d.get("profile"))))


def last_promote(profile: str) -> dict:
    demoted = {d.get("promote_receipt") for d in _receipts("demote")}
    cands = [r for r in _receipts("promote") if r.get("profile") == profile and r["id"] not in demoted]
    if not cands:
        raise HwsError(f"no un-demoted promote receipt for profile {profile!r}")
    return cands[-1]


def demote(profile: str, *, receipt_id: str | None = None, last: bool = False,
           i_am_owner: bool = False, dry_run: bool = False) -> dict:
    profiles.check_name(profile)
    _require_owner(profile, i_am_owner, "demote from", dry_run)
    if bool(receipt_id) == bool(last):
        raise UsageError("demote needs exactly one of --receipt ID or --last")
    rec = load_receipt(receipt_id) if receipt_id else last_promote(profile)
    if rec.get("kind") != "promote" or rec.get("profile") != profile:
        raise UsageError(f"receipt {rec.get('id')} is not a promote receipt for profile {profile!r}")
    target = rec["skills_dir"]
    names = [s["name"] for s in rec["skills"]]
    before = read_json(rec["backup"] + ".manifest.json", {})
    plan = {"kind": "demote", "profile": profile, "promote_receipt": rec["id"],
            "restored_from": rec["backup"], "remove": names}
    if dry_run:
        return {**plan, "dry_run": True}
    for n in names:
        p = os.path.join(target, n)
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p)
    wanted = set(names)
    with tarfile.open(rec["backup"], "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.split("/", 1)[0] in wanted]
        for m in members:
            if m.name.startswith("/") or ".." in m.name.split("/"):
                raise HwsError(f"unsafe member in backup: {m.name}")
        if hasattr(tarfile, "tar_filter"):  # 3.12+ (and security backports)
            tar.extractall(target, members=members, filter="tar")
        else:
            tar.extractall(target, members=members)
        usage_member = next((m for m in tar.getmembers() if m.name == USAGE_FILE and m.isfile()), None)
        backup_usage = tar.extractfile(usage_member).read() if usage_member else None
    usage = merge_usage(os.path.join(target, USAGE_FILE), backup_usage, names)
    after = manifest(target)
    ignored = sorted(k for k in after if k not in before and k.endswith(".lock"))
    diffs = sorted(k for k in set(before) | set(after)
                   if before.get(k) != after.get(k) and k not in ignored)
    ts = stamp()
    receipt = {**plan, "id": f"{ts}-demote-{profile}", "ts": now_iso(),
               "skills_dir_sha256_after": canonical_sha256(after),
               "manifest_before_sha256": rec.get("manifest_before_sha256"),
               "identical": not diffs, "diffs": diffs[:50], "ignored": ignored, "usage": usage}
    atomic_write_json(os.path.join(receipts_dir(), receipt["id"] + ".json"), receipt)
    return {**receipt, "dry_run": False}

