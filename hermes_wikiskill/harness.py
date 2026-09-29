"""Algorithm 1 orchestration (upstream harness.py) for Hermes.

    for k in next_iter..K:
      if R_best == 1.0: break
      train rollouts (S_{k-1})        → raw layer (+ TRAIN grader feedback, D18)
      stratified sample (≤5 fail, ≤3 pass)
      wiki maintenance (maintainer)   → wiki layer  (never rolled back)
      skill proposal (proposer)       → candidate S'_k (validated, contained: D11)
      apply proposal, gate on val     (every rollout must SEE the skills: D1)
      if R_val > R_best: accept, commit  else: roll back skills only (verified: D15)

Dry-run returns a plan and writes nothing (D3). Held-out test tasks never enter
this loop (D8). Rollout errors abort without a gate decision (D7).
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import stat
import tempfile

from . import agents, gating, gitutil, heldout, locking, prompts, resources, rollout, sessions, tasks as tasks_mod, traces, wiki
from .backends import hermes
from .errors import HwsError, UnsafePathError
from .util import atomic_write_json, atomic_write_text, now_iso, read_json, sha256_file


def ensure_framework_skills(ws: str) -> list[str]:
    """Stage the maintainer + proposer skills in the workspace (repairs broken copies)."""
    staged = []
    for name, src in resources.require_framework_skills():
        dst = os.path.join(ws, "skills", "framework", name)
        if os.path.islink(dst) or os.path.isfile(dst):
            os.remove(dst)
        elif os.path.isdir(dst) and not os.path.isfile(os.path.join(dst, "SKILL.md")):
            shutil.rmtree(dst)
        if not os.path.isdir(dst):
            shutil.copytree(src, dst)
        staged.append(dst)
    return staged


def init_workspace(ws: str, tasks: list[dict], *, dry_run: bool = False,
                   source_profile: str | None = None, model: str | None = None,
                   provider: str | None = None) -> dict:
    """Create a fresh evolution workspace with ``tasks`` (train/val only)."""
    tasks_mod.validate(tasks)
    resources.require_framework_skills()
    counts = {s: sum(1 for t in tasks if t["split"] == s) for s in tasks_mod.WORKSPACE_SPLITS}
    plan = {"ws": ws, "tasks": len(tasks), **counts,
            "creates": ["tasks.json", "bench/tasks/<id>/", "skills/active (git)",
                        "skills/framework/", "wiki/ (git)", "raw/traces/", "runs/proposals/",
                        f"{hermes.PROFILE_DIR}/ (key-less; runs use temporary homes)", "workspace.json"]}
    if dry_run:
        return {**plan, "dry_run": True}
    for d in ("raw/traces", "wiki/patterns", "skills/active", "skills/framework",
              "runs/proposals", "bench"):
        os.makedirs(os.path.join(ws, d), exist_ok=True)
    hermes.save_ws_settings(ws, backend="hermes", source_profile=source_profile,
                            model=model, provider=provider)
    gating.ensure_active_repo(ws)
    wiki.ensure(ws)
    prof = hermes.refresh_profile(ws, creds=False)  # R1-17: no provider-key copy stays in the ws
    ensure_framework_skills(ws)
    tasks_mod.replace_tasks(ws, tasks)
    return {**plan, "dry_run": False, "credentials": {"provider": prof["provider"],
                                                      "env": prof["env_names"],
                                                      "missing": prof["missing"]}}


def sample_traces(ws: str, it: int, train_results: list[dict],
                  max_fail: int = 5, max_pass: int = 3) -> list[dict]:
    """Stratified sampling per Appendix C: ≤5 failing + ≤3 passing TRAIN traces."""
    train_results = [r for r in train_results if r.get("split") == "train"]
    fails = [r for r in train_results if (r.get("score") or 0) < 1.0]
    passes = [r for r in train_results if (r.get("score") or 0) == 1.0]
    rng = random.Random(it)
    picked = (rng.sample(fails, min(max_fail, len(fails))) +
              rng.sample(passes, min(max_pass, len(passes))))
    out = []
    for r in picked:
        p = traces.transcript_path(ws, it, "train", r["id"])
        out.append({"task_id": r["id"], "score": r.get("score"), "title": r.get("title", ""),
                    "feedback": r.get("feedback"),
                    "path": p if os.path.exists(p) else traces.meta_path(ws, it, "train", r["id"])})
    return out


ROLE_TAGS = {"maintainer": "maintain", "proposer": "propose"}
_ATTEMPT = re.compile(r"attempt-(\d+)")


def role_run_dir(ws: str, role: str, k: int) -> str:
    return os.path.join(ws, "runs", f"{ROLE_TAGS[role]}-{k:02d}")


# R3-01: what the HARNESS writes into a role's own run dir (everything else there is role output)
HARNESS_RECORDS = ("query.txt", "stdout.txt", "stderr.txt", "session.jsonl", "run.json", "confinement.json")


def archive_attempt(run_dir: str) -> str | None:
    """R1-01 / INTERFACE §6: keep a previous attempt's evidence at ``<run_dir>/attempt-<k>/``
    instead of overwriting it; the final attempt stays at ``<run_dir>/run.json``.
    R3-01: only the harness records (regular files, HARNESS_RECORDS) are archived — anything else a
    role left in its own run dir is removed, never kept as a readable attempt file."""
    if not os.path.isdir(run_dir):
        return None
    entries = [n for n in os.listdir(run_dir) if not _ATTEMPT.fullmatch(n)]
    records = [n for n in entries if n in HARNESS_RECORDS and os.path.isfile(os.path.join(run_dir, n))
               and not os.path.islink(os.path.join(run_dir, n))]
    for n in entries:
        if n not in records:
            _q_remove(os.path.join(run_dir, n))
    if not records:
        return None
    nums = [int(m.group(1)) for m in map(_ATTEMPT.fullmatch, os.listdir(run_dir)) if m]
    dst = os.path.join(run_dir, f"attempt-{max(nums, default=0) + 1}")
    os.makedirs(dst)
    for n in records:
        os.replace(os.path.join(run_dir, n), os.path.join(dst, n))
    return dst


CONFINEMENT_FILE = "confinement.json"


def _record_present(run_dir: str, present: bool) -> None:
    """T9g: held-out data existed at role START — kept in the role run.json (moves with attempt-k)."""
    p = os.path.join(run_dir, "run.json")
    rec = read_json(p, {})
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_json(p, {**(rec if isinstance(rec, dict) else {}), "heldout_present": present})


def audit_role(ws: str, run_dir: str, *, res: dict | None = None,
               present: bool | None = None) -> dict[str, list[str]]:
    """Audit ONE role run right after it ended — also when it raised (R1-01). No exported
    session = unaudited (``{"unaudited": [...]}``). Contaminated runs are redacted in place.
    ``present``: held-out data existed at role start (T9g); None → the run.json record."""
    sf = os.path.join(run_dir, "session.jsonl")
    if not os.path.isfile(sf):
        return {"unaudited": [f"no exported session in {os.path.relpath(run_dir, ws)}"]}
    rec = read_json(os.path.join(run_dir, "run.json"), {})
    home = (res or {}).get("role_home") or (rec.get("role_home") if isinstance(rec, dict) else None)
    if present is None and isinstance(rec, dict) and isinstance(rec.get("heldout_present"), bool):
        present = rec["heldout_present"]
    hits = heldout.audit_session(sessions.load(sf), ws, ids=heldout.heldout_ids(ws),
                                 digests=heldout.sealed_digests(ws), home=home, present=present)
    if any(hits.get(c) for c, _ in heldout.CATEGORIES):
        rollout.redact_run_dir(run_dir, query=False)
        # T9d/T10g: diagnosable without a tap, yet PATH-FREE in the ws (a later role may read it):
        # digests + tool.key + kind + counts here; the plain paths go to an operator-only file
        outside = hits.get("outside_paths") or []
        atomic_write_json(os.path.join(run_dir, CONFINEMENT_FILE), {
            "outside": heldout.path_digests(outside),
            "counts": {c: len(hits.get(c) or []) for c, _ in heldout.CATEGORIES},
            "home": "role" if home else "operator",
            "diagnostic": _private_diagnostic(ws, run_dir, outside) if outside else None})
    return hits


def _private_diagnostic(ws: str, run_dir: str, outside: list[dict]) -> str | None:
    """T10g: the READABLE flagged paths, kept OUTSIDE the ws at
    $HWS_HOME/diagnostics/<ws-id>/<run>-<stamp>.json; the ws only records the slash-free id."""
    from .profiles import hws_home
    from .util import stamp
    real = os.path.realpath(ws)
    wsid = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(real))[:60] + "-" + \
        sessions.digest(real).split(":")[1][:12]
    name = f"{os.path.basename(run_dir)}-{stamp()}.json"
    try:
        d = os.path.join(hws_home(), "diagnostics", wsid)
        os.makedirs(d, mode=0o700, exist_ok=True)
        atomic_write_json(os.path.join(d, name), {"ws": real, "run": os.path.relpath(run_dir, ws),
                                                  "outside": outside})
    except OSError:
        return None
    return f"HWS_HOME diagnostics {wsid} {name}"


def _contaminated(hits: dict) -> bool:
    return any(hits.get(c) for c, _ in heldout.CATEGORIES)


def _repos(ws: str) -> list[str]:
    return [gating.active_dir(ws), wiki.wiki_dir(ws)]


def _meta_snapshot(ws: str) -> dict[str, dict]:
    return {r: gitutil.meta_snapshot(r) for r in _repos(ws)}


def _refs_only(diff: list[str]) -> bool:
    return all(d == "packed-refs" or d == "refs" or d.startswith("refs/") for d in diff)


def _meta_check(ws: str, snap: dict[str, dict], active_head: str | None) -> tuple[str | None, list[str]]:
    """QA-F10, run right after a role ends and BEFORE any git command: byte-compare the git
    metadata of skills/active and wiki with the pre-role snapshot (config/filters, attributes,
    hooks, HEAD, refs); a change is restored and makes the iteration invalid. Indexes are rebuilt
    (no assume-unchanged tricks) and skills/active HEAD must still be the harness-made commit.

    T9f: a WIKI change limited to refs/** + packed-refs is the role's own `git commit` of its wiki
    work: restored (its commits undone, working tree kept) and returned as a NOTE, not a void.
    Returns (invalid-note | None, notes)."""
    changed, notes = [], []
    wiki_repo = wiki.wiki_dir(ws)
    for repo, before in snap.items():
        diff = gitutil.meta_changed(repo, before)
        if diff:
            rel = [os.path.relpath(os.path.join(repo, ".git", d), ws) for d in diff[:5]]
            if repo == wiki_repo and _refs_only(diff):
                notes.append(f"own wiki commits undone ({', '.join(d for d in diff[:3])})"[:200])
            else:
                changed += rel
            gitutil.meta_restore(repo, before)
    for repo, before in snap.items():
        if before:
            gitutil.reset_index(repo)
    if active_head and gating.active_head(ws) != active_head:  # belt and braces (refs restored above)
        gating.git_active(ws, "reset", "-q", "--hard", active_head)
        changed.append("skills/active HEAD moved")
    if not changed:
        return None, notes
    return f"role modified git metadata ({', '.join(changed[:4])})"[:300], notes


def _guard_metadata(ws: str) -> None:
    """Resume after a hard-killed role (no snapshot survived): refuse repos whose git metadata
    is not what the harness writes, instead of running git inside them."""
    bad = {os.path.relpath(r, ws): gitutil.unsafe_metadata(r) for r in _repos(ws)}
    bad = {r: b for r, b in bad.items() if b}
    if bad:
        raise HwsError(f"git metadata modified outside the harness: {bad} — inspect and restore it "
                       f"(e.g. from a backup) before running again")


def _wiki_checkpoint(ws: str, state: dict, message: str) -> None:
    """Commit the wiki and record it as the last AUDITED wiki state (R1-01)."""
    wiki.commit(ws, message)
    state["wiki_audited"] = wiki.head(ws)
    gating.save_state(ws, state)


def _wiki_restore(ws: str, state: dict) -> bool:
    """R1-01 resume: anything after the last audited wiki commit — commits or uncommitted
    edits of an aborted role run — is discarded (and purged) before the next maintainer."""
    head = wiki.head(ws)
    if head is None:
        return False
    target = state.get("wiki_audited") or head
    if head == target and gitutil.is_clean(wiki.wiki_dir(ws), ignored=True):  # R2-03
        state["wiki_audited"] = target
        return False
    wiki.discard_since(ws, target)
    state["wiki_audited"] = target
    gating.save_state(ws, state)
    return True


def _cleanup_contaminated(ws: str, k: int) -> None:
    ppath = proposal_path(ws, k)
    if os.path.isfile(ppath):  # a proposal written after reading held-out data is itself tainted
        with open(ppath, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        with open(ppath, "w", encoding="utf-8") as f:
            json.dump({"action": "invalid", "redacted": sessions.digest(raw)}, f)
    legacy = hermes.profile_dir(ws)  # pre-T8 role runs used this home: drop its transcript stores
    for n in ("state.db", "state.db-wal", "state.db-shm", "sessions", "logs"):
        p = os.path.join(legacy, n)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
            os.makedirs(p, exist_ok=True)
        elif os.path.lexists(p):
            os.remove(p)


def _guard_active(ws: str) -> str | None:
    """R1-02: a role must never land edits in skills/active (only apply_proposal may)."""
    dirty = gating.active_dirty(ws)
    if not dirty:
        return None
    gating.reset_active(ws)
    return f"role wrote skills/active directly ({len(dirty)} path(s): {', '.join(dirty[:3])[:120]})"


def _recover_role(ws: str, state: dict) -> str | None:
    """Before any git command of a (resumed) run: refuse tampered git metadata, and if a role was
    hard-killed (``role_running`` survived: SIGKILL/OOM, no ``finally``) treat it as UNAUDITED —
    put skills/active HEAD back and quarantine its outputs from the surviving pre-role snapshot
    (R2-01, incl. this iteration's proposal file). Snapshot gone → fail closed: stale proposal
    deleted, wiki back to the last audited commit, skills/active reset, then HwsError naming the
    files that cannot be verified."""
    _guard_metadata(ws)
    rr = state.get("role_running")
    if not rr:
        return None
    rr = rr if isinstance(rr, dict) else {}
    msgs = []
    if rr.get("active_head") and gating.active_head(ws) != rr["active_head"]:
        gating.git_active(ws, "reset", "-q", "--hard", rr["active_head"])
        msgs.append(f"skills/active HEAD reset to {rr['active_head'][:12]} (moved by a killed {rr.get('role')} run)")
    snap_dir = _resolve_snapshot(rr.get("snapshot"))
    kk = rr.get("k") if isinstance(rr.get("k"), int) else state.get("next_iter")  # H1: legacy marker
    killed_dir = role_run_dir(ws, rr["role"], kk) if rr.get("role") in ROLE_TAGS and isinstance(kk, int) else None
    closed, unverifiable = False, None
    why = "left no pre-role snapshot"
    if snap_dir and os.path.isfile(os.path.join(snap_dir, QUARANTINE_MANIFEST)):
        try:  # H2 (invalid manifest) / H3 (missing copy) raise BEFORE any ws change
            q = _quarantine(ws, snap_dir)
            msgs.append(f"killed {rr.get('role')} run quarantined: {_q_counts(q)}")
            if killed_dir:  # T11c: its records count only by provenance; then evidence for retro_audit
                _vet_records(killed_dir, snap_dir, strict=True)
                _redact_streams(killed_dir)
                _record_quarantine(killed_dir, q)
                if isinstance(rr.get("started_ns"), int):
                    _record_fields(killed_dir, started_ns=rr["started_ns"])
            # R3-01 (c05): state.json is now the harness's pre-role copy — drop what was loaded
            # from the killed role's version; only harness updates are applied from here on
            state.clear()
            state.update(gating.load_state(ws))
        except HwsError as e:
            why = f"left an unusable pre-role snapshot ({e})"
            closed, unverifiable = True, _fail_closed(ws, state, rr)
    else:
        closed, unverifiable = True, _fail_closed(ws, state, rr)
    if closed and killed_dir:  # T11c: no provenance can be shown — no record name of that run is kept
        _vet_records(killed_dir, None, strict=True)
    state.pop("role_running", None)
    gating.save_state(ws, state)
    if snap_dir:
        shutil.rmtree(snap_dir, ignore_errors=True)
    if closed:
        what = ("unknown (no start time recorded)" if unverifiable is None else
                ", ".join(unverifiable[:10]) + (f" (+{len(unverifiable) - 10} more)" if len(unverifiable) > 10 else "")
                if unverifiable else "none changed since it started (deleted files cannot be detected)")
        raise HwsError(f"a killed {rr.get('role') or 'role'} run {why}: its wiki / skills/active "
                       f"changes and proposal were discarded, but files it may have written elsewhere in {ws} cannot be "
                       f"verified: {what}. Inspect (delete anything the role wrote), then re-run.")
    return "; ".join(msgs) or None


_SNAP_NAME = re.compile(r"hws-rolesnap-[A-Za-z0-9_]+")


def _resolve_snapshot(ref: object) -> str | None:
    """T10g (QA-F26): role_running.snapshot is a basename, resolved under the tmp dir only when it
    is exactly ours: `hws-rolesnap-*` name, a real dir (not a link), owned by us, mode 0700. A legacy
    absolute value is accepted only when it names such a dir directly under the tmp dir. Anything
    else → None (the fail-closed path)."""
    if not isinstance(ref, str):
        return None
    tmp = tempfile.gettempdir()
    if os.path.isabs(ref):
        if os.path.dirname(ref) != tmp:
            return None
        ref = os.path.basename(ref)
    if not _SNAP_NAME.fullmatch(ref):
        return None
    p = os.path.join(tmp, ref)
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        return None
    return p


def _fail_closed(ws: str, state: dict, rr: dict) -> list[str] | None:
    """Snapshot missing: undo what can be undone without it; return the files changed since the role
    started (ctime — not settable by the role), or None when no start time was recorded (legacy)."""
    k = rr.get("k")
    if not isinstance(k, int):  # H1: legacy marker — the killed role belonged to the next iteration
        k = state.get("next_iter") if isinstance(state.get("next_iter"), int) else None
    if isinstance(k, int):
        pp = proposal_path(ws, k)
        if os.path.lexists(pp):
            _q_remove(pp)
    if wiki.head(ws) is not None and state.get("wiki_audited"):
        wiki.discard_since(ws, state["wiki_audited"])
    if os.path.isdir(os.path.join(gating.active_dir(ws), ".git")) and gating.active_dirty(ws):
        gating.reset_active(ws)
    started = rr.get("started_ns")
    if not isinstance(started, int):
        return None
    own = os.path.relpath(role_run_dir(ws, rr.get("role") or "maintainer", k), ws) if isinstance(k, int) else "runs/-"
    files, _dirs = _q_walk(ws, own, "", hash_files=False)
    return sorted(r for r in files if r.split(os.sep)[0] != "wiki"
                  and os.lstat(os.path.join(ws, r)).st_ctime_ns >= started)


# ---------------------------------------------------------------- R2-01 role output quarantine
QUARANTINE_MANIFEST = "manifest.json"
SNAPSHOT_PREFIX = "hws-rolesnap-"  # host R6: the only allowed tmp writes are /tmp/hws-*
MANIFEST_VERSION = 2
_SHA = re.compile(r"[0-9a-f]{64}")


def _q_excluded(rel: str, own: str, proposal: str) -> bool:
    """R3-01 ALLOWLIST — the only paths a bad role run does NOT get restored: the harness records at
    the top of this run's own dir (HARNESS_RECORDS), runs/.role-stamp, the backend's role homes
    `.hws-roles/r-*`, this iteration's proposal (deleted on a bad run) and the skills/active WORKING
    tree (reset to HEAD by _guard_active/reset_active — keeps the R1-02 verdict). Everything else is
    snapshotted and restored: role files in its own run dir, other .hws-roles entries, the WHOLE
    wiki/.git and skills/active/.git trees, the lock and runs/state.json."""
    parts = rel.split(os.sep)
    if rel in (ROLE_STAMP, proposal):
        return True
    if parts[0] == hermes.ROLES_DIR and len(parts) > 1 and parts[1].startswith("r-"):
        return True
    if parts[:2] == ["skills", "active"] and len(parts) > 2 and parts[2] != ".git":
        return True
    return os.path.dirname(rel) == own and parts[-1] in HARNESS_RECORDS


def _mode(st: os.stat_result) -> int:
    return st.st_mode & 0o7777


def _q_walk(ws: str, own: str, proposal: str, *, hash_files: bool = True,
            repair: bool = False) -> tuple[dict[str, dict], dict[str, int]]:
    """{rel: {kind: file|link|other, size, mtime_ns, mode, sha256 | target}}, {dir rel: mode}
    (every entry, symlinks never followed). ``repair``: a dir the role made unlistable is made
    listable first (u+rwx) so nothing it created stays hidden (its mode is restored or it is removed)."""
    files: dict[str, dict] = {}
    dirs: dict[str, int] = {}
    for dirpath, dirnames, names in os.walk(ws):
        rdir = os.path.relpath(dirpath, ws)
        keep = []
        for d in dirnames:
            rel = os.path.normpath(os.path.join(rdir, d))
            if _q_excluded(rel, own, proposal):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                names.append(d)
                continue
            dirs[rel] = _mode(os.lstat(os.path.join(dirpath, d)))
            if repair and not os.access(os.path.join(dirpath, d), os.R_OK | os.W_OK | os.X_OK):
                os.chmod(os.path.join(dirpath, d), dirs[rel] | 0o700)
            keep.append(d)
        dirnames[:] = keep
        for n in names:
            rel = os.path.normpath(os.path.join(rdir, n))
            p = os.path.join(dirpath, n)
            if _q_excluded(rel, own, proposal):
                continue
            st = os.lstat(p)
            if os.path.islink(p):
                files[rel] = {"kind": "link", "target": os.readlink(p)}
            elif os.path.isfile(p):
                try:
                    sha = sha256_file(p) if hash_files else None
                except OSError:  # e.g. chmod 000 by the role: counts as changed
                    sha = "unreadable"
                files[rel] = {"kind": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                              "mode": _mode(st), "sha256": sha}
            else:  # fifo/socket/device: never legitimate ws content
                files[rel] = {"kind": "other"}
    return files, dirs


def _ws_snapshot(ws: str, role: str, k: int) -> str:
    """Pre-role snapshot + content copy in a private 0700 temp dir OUTSIDE the ws."""
    own = os.path.relpath(role_run_dir(ws, role, k), ws)
    proposal = os.path.relpath(proposal_path(ws, k), ws)
    tmp = tempfile.mkdtemp(prefix=SNAPSHOT_PREFIX, dir=tempfile.gettempdir())
    try:
        files, dirs = _q_walk(ws, own, proposal)
        for rel, e in files.items():
            if e["kind"] == "file":
                dst = os.path.join(tmp, "content", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(ws, rel), dst)
        atomic_write_json(os.path.join(tmp, QUARANTINE_MANIFEST),
                          {"version": MANIFEST_VERSION, "ws": os.path.realpath(ws), "own": own,
                           "proposal": proposal, "n_files": len(files), "files": files, "dirs": dirs})
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp


def _safe_rel(rel: object) -> bool:
    return (isinstance(rel, str) and rel != "" and not os.path.isabs(rel)
            and ".." not in rel.split(os.sep) and os.path.normpath(rel) == rel)


def _q_remove(p: str) -> None:
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.remove(p)


def _valid_entry(e: object) -> bool:
    if not isinstance(e, dict):
        return False
    if e.get("kind") == "file":
        return (isinstance(e.get("sha256"), str) and bool(_SHA.fullmatch(e["sha256"]))
                and isinstance(e.get("size"), int) and isinstance(e.get("mode"), int))
    if e.get("kind") == "link":
        return isinstance(e.get("target"), str)
    return e.get("kind") == "other"


def _load_manifest(ws: str, snap_dir: str) -> dict:
    """H2: a manifest that is unreadable, of another ws or malformed is never acted on (an empty or
    truncated one would make every ws file look "created")."""
    man = read_json(os.path.join(snap_dir, QUARANTINE_MANIFEST), None)
    files = man.get("files") if isinstance(man, dict) else None
    dirs = man.get("dirs") if isinstance(man, dict) else None
    ok = (isinstance(man, dict) and man.get("version") == MANIFEST_VERSION
          and man.get("ws") == os.path.realpath(ws)
          and _safe_rel(man.get("own")) and str(man["own"]).startswith("runs" + os.sep)
          and _safe_rel(man.get("proposal"))
          and isinstance(files, dict) and man.get("n_files") == len(files)
          and all(_safe_rel(r) and _valid_entry(e) for r, e in files.items())
          and isinstance(dirs, dict) and all(_safe_rel(d) and isinstance(m, int) for d, m in dirs.items()))
    if not ok:
        raise HwsError(f"role snapshot manifest in {os.path.basename(snap_dir)} (system tmp) is unreadable or invalid — nothing was deleted "
                       f"or restored in {ws}; inspect the workspace before running again")
    return man


def _quarantine(ws: str, snap_dir: str) -> dict[str, list[str]]:
    """Undo a bad role run outside wiki git / skills/active: delete what it CREATED (files, links,
    dirs), restore what it CHANGED or DELETED (content and mode) from the copy; this iteration's
    proposal file is deleted. Fails closed (HwsError, no change) when the manifest is invalid (H2)
    or a needed content copy is missing/corrupt (H3). Returns {removed, restored} (relpaths)."""
    man = _load_manifest(ws, snap_dir)
    own, proposal = man["own"], man["proposal"]
    before, bdirs = man["files"], man["dirs"]
    root = os.path.realpath(ws)
    for d in sorted(bdirs, key=lambda d: d.count(os.sep)):  # H4: a chmod 000 dir would hide its files
        p = os.path.join(ws, d)
        if os.path.isdir(p) and not os.path.islink(p) and _mode(os.lstat(p)) != bdirs[d]:
            os.chmod(p, bdirs[d])
    now, ndirs = _q_walk(ws, own, proposal, repair=True)

    def same(rel: str) -> bool:
        e, b = now.get(rel), before[rel]
        return (e is not None and e["kind"] == b["kind"]
                and (e.get("sha256"), e.get("size"), e.get("target")) == (b.get("sha256"), b.get("size"), b.get("target")))

    create = sorted(r for r, e in now.items() if r not in before or before[r]["kind"] != e["kind"])
    restore = sorted(r for r, b in before.items() if b["kind"] in ("file", "link") and (r in create or not same(r)))
    chmod = sorted(r for r, b in before.items() if b["kind"] == "file" and r not in restore
                   and now[r].get("mode") != b["mode"])
    missing = []  # H3: verify every copy BEFORE touching the ws
    for r in restore:
        if before[r]["kind"] == "file":
            src = os.path.join(snap_dir, "content", r)
            try:
                ok = os.path.isfile(src) and not os.path.islink(src) and sha256_file(src) == before[r]["sha256"]
            except OSError:
                ok = False
            if not ok:
                missing.append(r)
    if missing:
        raise HwsError(f"role snapshot {os.path.basename(snap_dir)} (system tmp) lacks a valid copy of {', '.join(missing[:10])}"
                       f"{f' (+{len(missing) - 10} more)' if len(missing) > 10 else ''} — nothing was deleted or "
                       f"restored in {ws}; restore these files by hand before running again")
    removed, restored = [], []
    for rel in create:
        _q_remove(os.path.join(ws, rel))
        removed.append(rel)
    for rel in sorted(set(ndirs) - set(bdirs), key=lambda d: -d.count(os.sep)):
        p = os.path.join(ws, rel)
        if os.path.isdir(p) and not os.path.islink(p):
            try:  # R3-01: a created dir can hold allowlisted harness records (own run dir) — never rmtree
                os.rmdir(p)
                removed.append(rel + os.sep)
            except OSError:
                pass
    for rel in restore:
        b, p = before[rel], os.path.join(ws, rel)
        parent = os.path.dirname(p)
        if os.path.lexists(parent) and not os.path.isdir(parent):
            _q_remove(parent)
        os.makedirs(parent, exist_ok=True)
        if not (os.path.realpath(parent) + os.sep).startswith(root + os.sep):
            continue  # never write through a link out of the ws
        src = os.path.join(snap_dir, "content", rel)
        if rel == locking.LOCK_NAME and b["kind"] == "file" and os.path.isfile(p) and not os.path.islink(p):
            _rewrite_in_place(p, src)  # R3-01: the flock we hold lives on this inode — keep it
        else:
            _q_remove(p)
            if b["kind"] == "link":
                os.symlink(b["target"], p)
            else:
                shutil.copyfile(src, p)
        if b["kind"] == "file":
            os.chmod(p, b["mode"])
        restored.append(rel)
    for rel in chmod:  # H4: mode-only change
        os.chmod(os.path.join(ws, rel), before[rel]["mode"])
        restored.append(rel)
    for d in sorted(bdirs, key=lambda d: d.count(os.sep)):  # dirs it deleted (incl. empty ones)
        p = os.path.join(ws, d)
        if not os.path.lexists(p):
            os.makedirs(p, exist_ok=True)
            os.chmod(p, bdirs[d])
    pp = os.path.join(ws, proposal)
    if os.path.lexists(pp):
        _q_remove(pp)
        removed.append(proposal)
    return {"removed": removed, "restored": restored}


def _rewrite_in_place(p: str, src: str) -> None:
    with open(src, "rb") as f:
        data = f.read()
    fd = os.open(p, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _vet_records(run_dir: str, snap_dir: str | None, *, strict: bool = False) -> list[str]:
    """T11c (QA-F31): a HARNESS_RECORDS name in a role's own run dir is trusted by PROVENANCE, not by
    name — only when its sha256 equals what the backend recorded in the private snapshot dir when it
    wrote it (backends.hermes.PROVENANCE_DIR). Anything else under a record name is role output and is
    removed; query.txt is put back from the harness's own copy. ``snap_dir`` None (no snapshot left):
    nothing can be shown → every record name is removed. No provenance file at all = the backend never
    started a role process in this attempt (an in-process runner) → nothing to vet, unless ``strict``
    (hard-kill resume: nothing the killed run left is trusted). Returns the names dropped or restored."""
    prov = read_json(os.path.join(snap_dir, hermes.PROVENANCE_FILE), None) if snap_dir else {}
    if prov is None and not strict:
        return []
    prov = prov if isinstance(prov, dict) else {}
    changed = []
    for n in HARNESS_RECORDS:
        p = os.path.join(run_dir, n)
        want = prov.get(n)
        try:
            ok = (isinstance(want, str) and os.path.isfile(p) and not os.path.islink(p)
                  and sha256_file(p) == want)
        except OSError:
            ok = False
        if ok:
            continue
        own = os.path.join(snap_dir, "own-" + n) if snap_dir else None
        try:
            restorable = bool(own and isinstance(want, str) and os.path.isfile(own) and sha256_file(own) == want)
        except OSError:
            restorable = False
        if not os.path.lexists(p) and not restorable:
            continue
        if os.path.lexists(p):
            _q_remove(p)
        if restorable:
            os.makedirs(run_dir, exist_ok=True)
            shutil.copyfile(own, p)
        changed.append(n)
    return changed


# T11e: the run.json fields run_role itself writes after the vetting point (_record_present,
# _record_fields, _record_quarantine) — the values it wrote overwrite any role key of the same name
HARNESS_RUN_FIELDS = {"heldout_present": bool, "started_ns": int, "quarantine": dict}


def _provenance_lost(snap_dir: str) -> bool:
    """T11d (QA-F32): a role process was started with provenance on, yet the provenance file is
    missing or unreadable — the records of that run cannot be shown to be the harness's."""
    if snap_dir not in hermes.PROVENANCE_EXPECTED:
        return False
    return not isinstance(read_json(os.path.join(snap_dir, hermes.PROVENANCE_FILE), None), dict)


def _fail_closed_records(run_dir: str, *, audited_confinement: bool) -> None:
    """T11d: a BAD run without usable provenance trusts no record name the harness did not write after
    the vetting point: query.txt becomes a digest stub (stdout/stderr already are, _redact_streams), a
    confinement.json the audit did not write is removed; run.json notes it (count-only, path-free)."""
    qp = os.path.join(run_dir, "query.txt")
    if os.path.lexists(qp):
        try:
            with open(qp, "rb") as f:
                digest = sessions.digest(f.read().decode("utf-8", "replace"))
        except OSError:
            digest = "unreadable"
        _q_remove(qp)
        atomic_write_text(qp, f"<redacted: query.txt without provenance {digest}>\n")
    cp = os.path.join(run_dir, CONFINEMENT_FILE)
    if not audited_confinement and os.path.lexists(cp):
        _q_remove(cp)
    # T11e (QA-F34): an interrupt before the backend's own writes leaves ROLE-written session.jsonl /
    # run.json — drop the session; run.json keeps only fields the harness itself wrote after the
    # vetting point, never merged into a role dict
    sp = os.path.join(run_dir, "session.jsonl")
    if os.path.lexists(sp):
        _q_remove(sp)
    rp = os.path.join(run_dir, "run.json")
    rec = read_json(rp, {}) if os.path.isfile(rp) and not os.path.islink(rp) else {}
    rec = rec if isinstance(rec, dict) else {}
    if os.path.lexists(rp):
        _q_remove(rp)
    atomic_write_json(rp, {**{k: rec[k] for k, t in HARNESS_RUN_FIELDS.items() if isinstance(rec.get(k), t)},
                           "provenance_unavailable": True})


def _redact_streams(run_dir: str) -> None:
    """T11c: after a BAD role run its stdout/stderr (hermes' output interleaved with anything the role
    wrote under those names) are kept only as digests."""
    for n in ("stdout.txt", "stderr.txt"):
        p = os.path.join(run_dir, n)
        if not os.path.isfile(p) or os.path.islink(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if text.startswith("<redacted:"):
            continue
        atomic_write_text(p, f"<redacted: bad role run {n} {sessions.digest(text)}>\n")


def _q_counts(q: dict[str, list[str]]) -> str:
    return f"{len(q['removed'])} created path(s) removed, {len(q['restored'])} file(s) restored"


ROLE_STAMP = os.path.join("runs", ".role-stamp")


def _role_stamp(ws: str) -> int:
    """T10d: the role start on the FILESYSTEM clock — st_ctime_ns of a fresh harness-owned stamp on
    the ws filesystem (Linux file times use the coarse clock, up to a jiffy behind time.time_ns(), so
    a file the role writes in its first ms would otherwise look older than the start). A new inode
    each time: the recorded number cannot be moved by a later touch of the stamp."""
    p = os.path.join(ws, ROLE_STAMP)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.lexists(p):
        st = os.lstat(p)
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):  # QA-F24: never rmtree it
            raise HwsError(f"{ROLE_STAMP} in {ws} is not a regular file (harness-owned role stamp) — "
                           f"inspect and remove it, then re-run")
        os.remove(p)  # a stale stamp or a link: replaced, never followed
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        return os.fstat(fd).st_ctime_ns
    finally:
        os.close(fd)


def _mark_role(ws: str, role: str | None, active_head: str | None = None,
               snapshot: str | None = None, k: int | None = None) -> int | None:
    """Set (returns the recorded started_ns) or clear ``state.role_running``."""
    st = gating.load_state(ws)
    started = None
    if role:
        started = _role_stamp(ws)
        # T10g (QA-F26): only the snapshot dir's BASENAME — never an out-of-ws path in a ws record
        st["role_running"] = {"role": role, "active_head": active_head,
                              "snapshot": os.path.basename(snapshot) if snapshot else None, "k": k,
                              "started_ns": started}
    else:
        st.pop("role_running", None)
    gating.save_state(ws, st)
    return started


STATE_REL = os.path.join("runs", "state.json")


def _restore_state(ws: str, snap_dir: str) -> None:
    """R3-01 (probe c05): after EVERY role run runs/state.json is the harness's pre-role copy (taken
    before role_running was set) — a role's own edits of it are never loaded. No valid copy → only
    role_running is cleared (the pre-T11 behaviour)."""
    man = read_json(os.path.join(snap_dir, QUARANTINE_MANIFEST), None)
    e = man.get("files", {}).get(STATE_REL) if isinstance(man, dict) and isinstance(man.get("files"), dict) else None
    p = os.path.join(ws, STATE_REL)
    if man is not None and e is None:  # no state before the role: none after it
        if os.path.lexists(p):
            _q_remove(p)
        return
    src = os.path.join(snap_dir, "content", STATE_REL)
    try:
        ok = (isinstance(e, dict) and e.get("kind") == "file" and os.path.isfile(src) and not os.path.islink(src)
              and sha256_file(src) == e.get("sha256"))
    except OSError:
        ok = False
    if not ok:
        _mark_role(ws, None)
        return
    if os.path.lexists(p) and not (os.path.isfile(p) and not os.path.islink(p)):
        _q_remove(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hws-tmp-", dir=os.path.dirname(p))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, e["mode"])
        os.replace(tmp, p)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _record_fields(run_dir: str, **fields) -> None:
    p = os.path.join(run_dir, "run.json")
    rec = read_json(p, {})
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_json(p, {**(rec if isinstance(rec, dict) else {}), **fields})


def run_role(ws: str, role: str, k: int, step, *, wiki_base: str | None) -> tuple[object, dict]:
    """Run a role step and audit it on EVERY exit path. On a raise: its wiki changes since
    ``wiki_base`` and any skills/active edit are discarded, then the error propagates.
    Git metadata is checked first (QA-F10); a change is reported as ``hits["git_meta"]``, an
    undone own wiki commit (T9f) as ``hits["git_notes"]`` (also recorded in the role run.json).
    R2-01: a contaminated / unaudited / tampering / raising run is QUARANTINED from a pre-role
    snapshot (``hits["quarantine_notes"]``, run.json ``quarantine``)."""
    _guard_metadata(ws)
    snap = _meta_snapshot(ws)
    active_head = gating.active_head(ws)
    present = heldout.heldout_present(ws)  # T9g: BEFORE the step (a role deleting eval/ hides nothing)
    rdir = role_run_dir(ws, role, k)
    archive_attempt(rdir)  # R3-01: BEFORE the snapshot, so a restore never undoes the harness's archiving
    qdir = _ws_snapshot(ws, role, k)
    try:
        started = _mark_role(ws, role, active_head, qdir, k)
        hermes.PROVENANCE_DIR = qdir  # T11c: the backend records what it writes into rdir
        try:
            out = step()
        except BaseException:
            hermes.PROVENANCE_DIR = None
            _vet_records(rdir, qdir)
            _meta_check(ws, snap, active_head)  # the raise path discards the wiki changes anyway
            _record_present(rdir, present)
            _record_fields(rdir, started_ns=started)
            raised_hits = audit_role(ws, rdir, present=present)
            _record_quarantine(rdir, _quarantine(ws, qdir))  # incl. state.json (pre-role copy)
            if wiki_base:
                wiki.discard_since(ws, wiki_base)
            if gating.active_dirty(ws):
                gating.reset_active(ws)
            _redact_streams(rdir)
            if _provenance_lost(qdir):
                _fail_closed_records(rdir, audited_confinement=_contaminated(raised_hits))
            _restore_state(ws, qdir)
            raise
        hermes.PROVENANCE_DIR = None
        _vet_records(rdir, qdir)
        meta, gnotes = _meta_check(ws, snap, active_head)
        res = out[1] if isinstance(out, tuple) else out
        _record_present(rdir, present)
        _record_fields(rdir, started_ns=started)
        hits = audit_role(ws, rdir, res=res, present=present)
        if gnotes:
            rec = read_json(os.path.join(rdir, "run.json"), {})
            if isinstance(rec, dict):
                atomic_write_json(os.path.join(rdir, "run.json"), {**rec, "git_notes": gnotes})
            hits = {**hits, "git_notes": [f"{role}: {n}" for n in gnotes]}
        if _contaminated(hits) or hits.get("unaudited") or meta:
            q = _quarantine(ws, qdir)
            _record_quarantine(rdir, q)
            if q["removed"] or q["restored"]:
                hits = {**hits, "quarantine_notes": [f"{role} quarantined: {_q_counts(q)}"]}
            _redact_streams(rdir)
            if _provenance_lost(qdir):
                _fail_closed_records(rdir, audited_confinement=_contaminated(hits))
        _restore_state(ws, qdir)
    finally:
        hermes.PROVENANCE_DIR = None
        hermes.PROVENANCE_EXPECTED.discard(qdir)
        shutil.rmtree(qdir, ignore_errors=True)
    return out, ({**hits, "git_meta": [meta]} if meta else hits)


def _record_quarantine(run_dir: str, q: dict[str, list[str]]) -> None:
    p = os.path.join(run_dir, "run.json")
    rec = read_json(p, {})
    os.makedirs(run_dir, exist_ok=True)
    atomic_write_json(p, {**(rec if isinstance(rec, dict) else {}),
                          "quarantine": {"removed": q["removed"][:500], "restored": q["restored"][:500]}})


def maintain_step(ws: str, k: int, sampled: list[dict], runner=agents.run_agent,
                  dry_run: bool = False) -> dict:
    if not dry_run:
        ensure_framework_skills(ws)
        archive_attempt(role_run_dir(ws, "maintainer", k))
    return runner(ws, prompts.maintainer_prompt(ws, k, sampled), tag=f"maintain-{k:02d}",
                  toolsets=hermes.MAINTAINER_TOOLSETS, include_framework=True,
                  dry_run=dry_run, max_turns=60, run_budget=1500, role=True)


def proposal_path(ws: str, k: int) -> str:
    return os.path.join(ws, "runs", "proposals", f"iter-{k:02d}.json")


def propose_step(ws: str, k: int, train_results: list[dict], runner=agents.run_agent,
                 dry_run: bool = False) -> tuple[dict | None, dict, str | None]:
    """Run the proposer; returns (proposal | None, run result, invalid-note | None)."""
    if not dry_run:
        ensure_framework_skills(ws)
    ppath = proposal_path(ws, k)
    if not dry_run and os.path.exists(ppath):
        os.remove(ppath)  # never gate a stale proposal from an aborted earlier attempt
    if not dry_run:
        archive_attempt(role_run_dir(ws, "proposer", k))
    res = runner(ws, prompts.proposer_prompt(ws, k, train_results), tag=f"propose-{k:02d}",
                 toolsets=hermes.PROPOSER_TOOLSETS, include_framework=True,
                 dry_run=dry_run, max_turns=60, run_budget=1800, role=True)
    if dry_run or not os.path.exists(ppath):
        return None, res, None
    try:
        with open(ppath, encoding="utf-8") as f:
            proposal = json.load(f)
        gating.validate_proposal(proposal)
    except (OSError, ValueError) as e:  # UnsafePathError is a ValueError
        try:
            with open(ppath, encoding="utf-8", errors="replace") as f:
                raw = f.read(4000)
        except OSError:
            raw = ""
        return {"action": "invalid", "raw": raw}, res, f"{e.__class__.__name__}: {e}"
    return proposal, res, None


def maintain_audited(ws: str, k: int, sampled: list[dict], *, runner=agents.run_agent) -> tuple[dict, str | None]:
    """Manual ``maintain`` verb (R1-01): same audit as evolve. Returns (run result, note); a
    note means the run's wiki changes were discarded (contaminated / unaudited / wrote skills)."""
    state = gating.load_state(ws)
    _recover_role(ws, state)
    wiki.ensure(ws)
    _wiki_restore(ws, state)
    _guard_active(ws)
    base = wiki.head(ws)
    res, hits = run_role(ws, "maintainer", k, lambda: maintain_step(ws, k, sampled, runner=runner),
                         wiki_base=base)
    note = heldout.contamination_note({"maintainer": hits}) if _contaminated(hits) else None
    if note is None and hits.get("unaudited"):
        note = f"unaudited: {hits['unaudited'][0]}"
    if hits.get("git_meta"):
        note = f"{note} | {hits['git_meta'][0]}" if note else hits["git_meta"][0]
    wrote = _guard_active(ws)
    if wrote:
        note = f"{note} | {wrote}" if note else wrote
    if note and hits.get("quarantine_notes"):
        note = f"{note} | {hits['quarantine_notes'][0]}"
    if note:
        wiki.discard_since(ws, base)
        state["wiki_audited"] = wiki.head(ws)
        gating.save_state(ws, state)
    else:
        _wiki_checkpoint(ws, state, f"iter-{k:02d}: maintainer updates (manual)")
    return res, note


def propose_audited(ws: str, k: int, train_results: list[dict], *,
                    runner=agents.run_agent) -> tuple[dict | None, dict, str | None]:
    """Manual ``propose`` verb (R1-01): a contaminated / unaudited proposer run, or one that
    edited skills/active, leaves an INVALID proposal file (content never kept) and no edits."""
    state = gating.load_state(ws)
    _recover_role(ws, state)
    wiki.ensure(ws)
    _wiki_restore(ws, state)
    _guard_active(ws)
    base = wiki.head(ws)
    (proposal, res, note), hits = run_role(
        ws, "proposer", k, lambda: propose_step(ws, k, train_results, runner=runner), wiki_base=base)
    bad = heldout.contamination_note({"proposer": hits}) if _contaminated(hits) else None
    if bad is None and hits.get("unaudited"):
        bad = f"unaudited: {hits['unaudited'][0]}"
    if hits.get("git_meta"):
        bad = f"{bad} | {hits['git_meta'][0]}" if bad else hits["git_meta"][0]
    wrote = _guard_active(ws)
    if wrote:
        bad = f"{bad} | {wrote}" if bad else wrote
    if bad and hits.get("quarantine_notes"):
        bad = f"{bad} | {hits['quarantine_notes'][0]}"
    if bad:
        wiki.discard_since(ws, base)
        state["wiki_audited"] = wiki.head(ws)
        gating.save_state(ws, state)
        _cleanup_contaminated(ws, k)
        ppath = proposal_path(ws, k)
        if not os.path.isfile(ppath):
            os.makedirs(os.path.dirname(ppath), exist_ok=True)
            with open(ppath, "w", encoding="utf-8") as f:
                json.dump({"action": "invalid"}, f)
        return {"action": "invalid"}, res, bad[:600]
    return proposal, res, note


def plan_evolve(ws: str, iters: int, *, max_turns: int = 15, no_early_stop: bool = False) -> dict:
    """Side-effect-free description of what ``evolve`` would do (D3)."""
    sp = tasks_mod.splits(ws)
    state = gating.load_state(ws)
    iterations = list(range(state.get("next_iter", 1), iters + 1))
    stop = state.get("r_best") == 1.0 and not no_early_stop
    sample = None
    probe = (sp["val"] or sp["train"])[:1]
    if probe:
        r = gating.run_task(ws, probe[0], 0, dry_run=True, runner_kwargs={"max_turns": max_turns})
        sample = r["result"].get("cmd")
    return {"dry_run": True, "ws": ws, "train": len(sp["train"]), "val": len(sp["val"]),
            "baseline_needed": state.get("baseline") is None, "iterations": [] if stop else iterations,
            "early_stop": stop, "state": state, "sample_cmd": sample,
            "rollouts_per_iteration": len(sp["train"]) + len(sp["val"]) + 2}


def _attempt_notes(ws: str, role: str, k: int) -> list[str]:
    """R2-04: earlier attempts of this role run that were voided (recorded confinement.json)."""
    rdir = role_run_dir(ws, role, k)
    out = []
    for n in sorted(os.listdir(rdir) if os.path.isdir(rdir) else [],
                    key=lambda n: int(m.group(1)) if (m := _ATTEMPT.fullmatch(n)) else -1):
        if not _ATTEMPT.fullmatch(n):
            continue
        conf = read_json(os.path.join(rdir, n, CONFINEMENT_FILE), None)
        counts = (conf or {}).get("counts") or {} if isinstance(conf, dict) else {}
        cats = [c for c, v in counts.items() if v]
        if cats:
            out.append(f"{role} {n} voided before this resume ({', '.join(cats)})")
    return out


def _history(k, train_mean, r_val, accepted, desc, r_best_before, **extra) -> dict:
    return {"iter": k, "train_mean": train_mean, "r_val": r_val, "accepted": accepted,
            "proposal": desc, "r_best_before": r_best_before, "skills_visible": True,
            "ts": now_iso(), **extra}


def evolve(ws: str, iters: int = 3, model: str | None = None, provider: str | None = None,
           runner=agents.run_agent, dry_run: bool = False, verbose: bool = True,
           max_turns: int = 15, no_early_stop: bool = False) -> dict:
    if dry_run:
        return plan_evolve(ws, iters, max_turns=max_turns, no_early_stop=no_early_stop)

    def log(msg: str) -> None:
        if verbose:
            print(f"[hermes-wikiskill] {msg}", flush=True)

    rkw = {"max_turns": max_turns}
    sp = tasks_mod.splits(ws)
    train, val = sp["train"], sp["val"]
    if not val:
        raise HwsError("no val tasks: the gate needs a validation split")
    state = gating.load_state(ws)
    if model or provider:
        hermes.save_ws_settings(ws, model=model, provider=provider)
        log(f"workspace model → {model or '(unchanged)'} (provider: {provider or 'unchanged'})")
    moved = _recover_role(ws, state)  # QA-F10: before the first git command of this run
    if moved:
        log(moved)
    wiki.ensure(ws)
    ensure_framework_skills(ws)

    if state.get("baseline") is None:
        log(f"baseline validation: {len(val)} val tasks, current skill set")
        gate0 = gating.run_gate(ws, val, 0, runner=runner, overwrite=True, runner_kwargs=rkw)
        state["baseline"] = state["r_best"] = gate0["mean"]
        state["baseline_ts"] = now_iso()
        state["baseline_escapes"] = gate0["escapes"]
        wiki.append_log(ws, f"iter-00 baseline: R={gate0['mean']} ({len(val)} val tasks)")
        gating.save_state(ws, state)
        _wiki_checkpoint(ws, state, "iter-00: baseline")

    for k in range(state["next_iter"], iters + 1):
        if state["r_best"] == 1.0 and not no_early_stop:
            log("R_best == 1.0 → early stop")
            break
        moved = _recover_role(ws, state)  # QA-F10: before any git command
        if moved:
            log(f"iter {k}: {moved}")
        if _wiki_restore(ws, state):  # R1-01: an aborted earlier attempt left unaudited wiki changes
            log(f"iter {k}: discarded unaudited wiki changes (back to {state['wiki_audited'][:12]})")
        if _guard_active(ws):
            log(f"iter {k}: discarded uncommitted skills/active changes of an aborted run")
        log(f"iter {k}/{iters}: train rollouts ({len(train)} tasks)")
        train_results = [gating.run_task(ws, t, k, runner=runner, overwrite=True, runner_kwargs=rkw)
                         for t in train]
        train_mean = gating.mean_score(train_results)
        train_escapes = sum(len(r.get("escapes") or []) for r in train_results)

        sampled = sample_traces(ws, k, train_results)
        log(f"iter {k}: wiki maintenance (sampled {len(sampled)} traces)")
        wiki_before = wiki.head(ws)
        found: dict[str, dict] = {}
        _, mhits = run_role(ws, "maintainer", k, lambda: maintain_step(ws, k, sampled, runner=runner),
                            wiki_base=wiki_before)
        tamper = (mhits.get("git_meta") or [None])[0]
        rnotes = list(mhits.get("git_notes") or [])  # T9f: e.g. the maintainer's own wiki commits undone
        rnotes += _attempt_notes(ws, "maintainer", k)  # R2-04: a raise-path void of an earlier attempt
        rnotes += mhits.get("quarantine_notes") or []  # R2-01
        if _contaminated(mhits):  # audited BEFORE anything is committed
            found["maintainer"] = mhits
            wiki.discard_since(ws, wiki_before)
        elif tamper:
            wiki.discard_since(ws, wiki_before)
        elif mhits.get("unaudited"):  # no session to audit → its wiki changes are never committed
            log(f"iter {k}: maintainer run unaudited ({mhits['unaudited'][0]}); wiki changes discarded")
            wiki.discard_since(ws, wiki_before)
            state["wiki_audited"] = wiki.head(ws)
            gating.save_state(ws, state)
        else:
            _wiki_checkpoint(ws, state, f"iter-{k:02d}: maintainer updates")
        wiki_after = wiki.head(ws)

        proposal, note = None, None
        if not found and not tamper:
            log(f"iter {k}: skill proposal")
            (proposal, _, note), phits = run_role(
                ws, "proposer", k, lambda: propose_step(ws, k, train_results, runner=runner),
                wiki_base=wiki_after)
            tamper = (phits.get("git_meta") or [None])[0]
            rnotes += phits.get("git_notes") or []
            rnotes += _attempt_notes(ws, "proposer", k)
            rnotes += phits.get("quarantine_notes") or []  # R2-01
            if _contaminated(phits):
                found["proposer"] = phits
            elif phits.get("unaudited") and not tamper:  # R2-01: its (quarantined) proposal is never gated
                tamper = f"unaudited: {phits['unaudited'][0]}"
            if _contaminated(phits) or tamper:
                wiki.discard_since(ws, wiki_after)
        prev_best = state["r_best"]
        contaminated = heldout.contamination_note(found)
        if contaminated:
            log(f"iter {k}: CONTAMINATED — {contaminated}; proposal invalid, no gate run")
            _cleanup_contaminated(ws, k)
            proposal, note = {"action": "invalid"}, contaminated[:600]  # its content is never recorded
        if tamper:
            log(f"iter {k}: INVALID — {tamper}; restored, no gate run")
            proposal = {"action": "invalid"}
            note = f"{note} | {tamper}" if note else tamper
        nsfx = f" [note: {'; '.join(rnotes)}]" if rnotes else ""
        nextra = {"role_notes": rnotes} if rnotes else {}
        if rnotes:
            log(f"iter {k}: note — {'; '.join(rnotes)}")
        wrote_active = _guard_active(ws)
        if wrote_active:
            log(f"iter {k}: INVALID — {wrote_active}; reset to HEAD, no gate run")
            proposal = {"action": "invalid"}
            note = f"{note} | {wrote_active}" if note else wrote_active
        if proposal is None or proposal.get("action") == "no_action":
            wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
                ws, k, {"action": "no_action"}, None, False, "", None))
            wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} proposal=no_action{nsfx}")
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            _wiki_checkpoint(ws, state, f"iter-{k:02d}: no_action")
            continue

        base = None
        if note is None:
            base = gating.commit_base(ws, k)
            try:
                desc = gating.apply_proposal(ws, proposal)
            except (ValueError, OSError) as e:
                note = f"{e.__class__.__name__}: {e}"
        if note is not None:  # invalid / unsafe / unappliable / contaminated: no validation run
            if base:
                gating.rollback(ws, base)
            log(f"iter {k}: proposal rejected before validation ({note})")
            state["history"].append(_history(k, train_mean, None, False, f"invalid: {note}"[:600],
                                             prev_best, escapes=train_escapes, **nextra))
            state["next_iter"] = k + 1
            gating.save_state(ws, state)
            wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
                ws, k, proposal, None, False, "", prev_best, note=note))
            wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} proposal invalid: {note}{nsfx}")
            _wiki_checkpoint(ws, state, f"iter-{k:02d}: invalid proposal")
            continue

        try:
            diff = gating.skill_diff(ws)
            log(f"iter {k}: validation gating ({len(val)} val tasks) — {desc}")
            gatek = gating.run_gate(ws, val, k, runner=runner, overwrite=True, runner_kwargs=rkw)
        except BaseException:
            gating.rollback(ws, base)  # no decision without a complete, visible gate
            raise
        r_val = gatek["mean"]
        accepted = r_val > prev_best
        if accepted:
            commit = gating.accept_commit(ws, k, r_val)
            state["r_best"] = r_val
        else:
            gating.rollback(ws, base)
            commit = base
        state["history"].append(_history(k, train_mean, r_val, accepted, desc, prev_best,
                                         active_commit=commit, escapes=train_escapes + gatek["escapes"],
                                         **nextra))
        state["next_iter"] = k + 1
        gating.save_state(ws, state)
        wiki.append_skill_impact(ws, prompts.gate_outcome_entry(
            ws, k, proposal, r_val, accepted, diff, prev_best))
        wiki.append_log(ws, f"iter-{k:02d}: train={train_mean} propose={desc} R_val={r_val} "
                            f"{'ACCEPT' if accepted else 'reject'} (R_best={state['r_best']}){nsfx}")
        _wiki_checkpoint(ws, state, f"iter-{k:02d}: gate outcome ({'accept' if accepted else 'reject'}, R_val={r_val})")
        log(f"iter {k}: R_val={r_val} → {'ACCEPTED' if accepted else 'REJECTED (rolled back)'} "
            f"(R_best={state['r_best']})")
    return state


__all__ = ["init_workspace", "evolve", "plan_evolve", "sample_traces", "maintain_step",
           "propose_step", "ensure_framework_skills", "UnsafePathError", "audit_role", "run_role",
           "archive_attempt", "role_run_dir", "maintain_audited", "propose_audited"]
