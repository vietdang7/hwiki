"""Checked git calls for skills/active and wiki/ (D15: upstream ignored return codes)."""

from __future__ import annotations

import os
import shutil
import subprocess

from .errors import GitError

# QA-F10: skills/active/.git and wiki/.git are writable by the roles (cwd = ws) — every framework
# git call neutralises hooks AND fsmonitor (a role-set core.fsmonitor would run inside `git status`
# and could lie about the tree); the per-role metadata snapshot below covers config/filters.
IDENT = ["-c", "user.email=hermes-wikiskill@local", "-c", "user.name=hermes-wikiskill",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]


def _env() -> dict[str, str]:
    """No inherited GIT_* redirection (GIT_DIR, GIT_CONFIG_*, ...), no system/global config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # T10c: a role-created refs/replace/* must never substitute the gated commit's objects
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    return env


def git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=_env())
    except OSError as e:
        raise GitError(f"git not runnable: {e}") from None
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed in {cwd} (exit {p.returncode}): "
                       f"{(p.stderr or p.stdout).strip()[:400]}")
    return p


def init_repo(cwd: str, message: str) -> None:
    git(cwd, "init", "-q")
    git(cwd, "commit", "-q", "--allow-empty", "-m", message)


def head(cwd: str) -> str:
    return git(cwd, "rev-parse", "HEAD").stdout.strip()


def nested_git(cwd: str) -> list[str]:
    """T11 m1: every ``.git`` entry (dir, file or link) BELOW the repo top — a nested repository.
    git status skips it (inside a tracked dir it is not even reported) and ``clean -fdx`` keeps it."""
    out = []
    for dirpath, dirnames, names in os.walk(cwd):
        top = dirpath == cwd
        for n in list(dirnames) + names:
            if n == ".git" and not top:
                out.append(os.path.relpath(os.path.join(dirpath, n), cwd))
        dirnames[:] = [d for d in dirnames if d != ".git" and not os.path.islink(os.path.join(dirpath, d))]
    return sorted(out)


def remove_nested_git(cwd: str) -> list[str]:
    """T11 m1: delete nested repositories' ``.git`` entries (links never followed)."""
    gone = nested_git(cwd)
    for rel in gone:
        p = os.path.join(cwd, rel)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.remove(p)
    return gone


def is_clean(cwd: str, *, ignored: bool = False) -> bool:
    """No tracked/untracked changes; ``ignored`` also counts git-ignored files (R2-03); a nested
    repository (T11 m1) is never clean."""
    extra = ("--ignored",) if ignored else ()
    return (git(cwd, "status", "--porcelain", "--untracked-files=all", *extra).stdout.strip() == ""
            and not nested_git(cwd))


_BLOB_MODES = {"100644": 0o644, "100755": 0o755}


def _git_bytes(cwd: str, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        p = subprocess.run(["git", *IDENT, *args], cwd=cwd, capture_output=True, input=stdin, env=_env())
    except OSError as e:
        raise GitError(f"git not runnable: {e}") from None
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed in {cwd} (exit {p.returncode}): "
                       f"{p.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return p.stdout


def export_tree(cwd: str, rev: str, dst: str) -> list[str]:
    """Write the files of commit ``rev`` into ``dst`` straight from the object store (R2-05/T10c):
    `ls-tree` + `cat-file`, so no working tree/index, no attributes (export-ignore/subst), no
    filters, and no tarfile (works on every supported Python). Only regular blobs are written;
    a symlink / submodule entry or an unsafe path is refused (UnsafePathError). Returns relpaths."""
    from .errors import UnsafePathError
    listing = _git_bytes(cwd, "ls-tree", "-r", "-z", "--full-tree", f"{rev}^{{tree}}")
    entries = []
    for rec in filter(None, listing.split(b"\0")):
        meta, _, raw = rec.partition(b"\t")
        mode, typ, sha = meta.decode().split()
        rel = raw.decode("utf-8", "surrogateescape")
        parts = rel.split("/")
        if (typ != "blob" or mode not in _BLOB_MODES or rel.startswith("/")
                or any(x in ("", ".", "..", ".git") for x in parts)):
            raise UnsafePathError(f"skills/active {rev[:12]}: entry {rel!r} (mode {mode}, {typ}) refused — "
                                  f"only regular files are exported")
        entries.append((mode, sha, rel))
    if not entries:
        return []
    out = _git_bytes(cwd, "cat-file", "--batch", stdin="".join(f"{sha}\n" for _m, sha, _r in entries).encode())
    pos, written = 0, []
    root = os.path.realpath(dst)
    for mode, sha, rel in entries:
        nl = out.index(b"\n", pos)
        hsha, htyp, hsize = out[pos:nl].decode().split()
        if hsha != sha or htyp != "blob":
            raise GitError(f"cat-file returned {hsha} {htyp} for {sha}")
        size = int(hsize)
        data, pos = out[nl + 1:nl + 1 + size], nl + 1 + size + 1
        p = os.path.join(dst, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not (os.path.realpath(os.path.dirname(p)) + os.sep).startswith(root + os.sep) or os.path.lexists(p):
            raise UnsafePathError(f"skills/active {rev[:12]}: entry {rel!r} refused")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), _BLOB_MODES[mode])
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        written.append(rel)
    return written


def commit_exists(cwd: str, sha: object) -> bool:
    return (isinstance(sha, str) and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
            and git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0)


# ---------------------------------------------------------------- QA-F10: role-proof git metadata
META = ("config", "HEAD", "packed-refs", "commondir", "info", "hooks", "refs", "objects/info")
SAFE_CONFIG = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
                         "core.ignorecase", "core.precomposeunicode", "core.symlinks"})
Entry = tuple  # ("d",) | ("l", target) | ("f", bytes)


def _entry(p: str) -> Entry | None:
    if os.path.islink(p):
        return ("l", os.readlink(p))
    if os.path.isdir(p):
        return ("d",)
    if os.path.isfile(p):
        with open(p, "rb") as f:
            return ("f", f.read())
    return None


def meta_snapshot(repo: str) -> dict[str, Entry]:
    """Bytes of the ``.git`` metadata a role could weaponise or use to move history (config,
    info/attributes, hooks, HEAD, refs, packed-refs, alternates). Reads files only — no git."""
    g = os.path.join(repo, ".git")
    top = _entry(g)
    if top is None:
        return {}
    out: dict[str, Entry] = {".git": top}
    if top != ("d",):
        return out
    for name in META:
        p = os.path.join(g, name)
        e = _entry(p)
        if e is None:
            continue
        out[name] = e
        if e == ("d",):
            for d, dirs, files in os.walk(p):
                for n in dirs + files:
                    fp = os.path.join(d, n)
                    out[os.path.relpath(fp, g)] = _entry(fp)
    return out


def meta_changed(repo: str, snap: dict[str, Entry]) -> list[str]:
    cur = meta_snapshot(repo)
    return sorted(k for k in set(cur) | set(snap) if cur.get(k) != snap.get(k))


def _remove(p: str) -> None:
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.remove(p)


def meta_restore(repo: str, snap: dict[str, Entry]) -> None:
    """Put the snapshot bytes back (added files removed). Fails closed if ``.git`` itself was
    replaced (its objects cannot be restored from a metadata snapshot)."""
    g = os.path.join(repo, ".git")
    if snap.get(".git") != ("d",) or _entry(g) != ("d",):
        raise GitError(f"{g} was replaced during a role run; restore the workspace from a backup")
    cur = meta_snapshot(repo)
    for rel in sorted(set(cur) - set(snap), key=len, reverse=True):
        _remove(os.path.join(g, rel))
    for rel in sorted(snap, key=len):
        if rel == ".git" or _entry(os.path.join(g, rel)) == snap[rel]:
            continue
        p, e = os.path.join(g, rel), snap[rel]
        _remove(p)
        if e[0] == "d":
            os.makedirs(p, exist_ok=True)
        elif e[0] == "l":
            os.symlink(e[1], p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(e[1])


def reset_index(repo: str) -> None:
    """Rebuild the index from HEAD: drops assume-unchanged / skip-worktree bits a role set (they
    hide working-tree edits from `git status`); the working tree is untouched."""
    idx = os.path.join(repo, ".git", "index")
    if os.path.lexists(idx):
        os.remove(idx)
    git(repo, "read-tree", "HEAD")


def unsafe_metadata(repo: str) -> list[str]:
    """Static check (no snapshot needed, e.g. on resume after a SIGKILLed role): config keys
    outside the plain `git init` set, attributes/alternates files, non-sample hooks."""
    g = os.path.join(repo, ".git")
    if not os.path.lexists(g):
        return []
    if _entry(g) != ("d",):
        return [".git (not a directory)"]
    bad = []
    cfg = os.path.join(g, "config")
    if os.path.isfile(cfg):
        p = git(repo, "config", "--file", cfg, "--no-includes", "--name-only", "--list", check=False)
        bad += [f"config {k}" for k in p.stdout.split() if k.lower() not in SAFE_CONFIG]
    for rel in ("info/attributes", "info/grafts", "objects/info/alternates", "objects/info/http-alternates",
                "commondir"):
        if os.path.lexists(os.path.join(g, rel)):
            bad.append(rel)
    # T10c: replace refs (loose or packed) substitute objects for every git command that honours them
    rdir = os.path.join(g, "refs", "replace")
    if os.path.lexists(rdir):
        found = [os.path.relpath(os.path.join(dp, n), g) for dp, _dn, fn in os.walk(rdir) for n in fn]
        bad += sorted(found) or ["refs/replace"]
    packed = os.path.join(g, "packed-refs")
    if os.path.isfile(packed):
        with open(packed, encoding="utf-8", errors="replace") as f:
            bad += [ln.split()[1] for ln in f if len(ln.split()) >= 2 and ln.split()[1].startswith("refs/replace/")]
    hooks = os.path.join(g, "hooks")
    if os.path.isdir(hooks):
        bad += [f"hooks/{n}" for n in sorted(os.listdir(hooks)) if not n.endswith(".sample")]
    return bad
