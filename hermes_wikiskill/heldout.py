"""Held-out / confinement guard for maintainer + proposer (QA-F7/F8/F9, SPEC R4).

Receipts: a live maintainer read ``<ws>/eval/<tag>/runs/rep-1/<test-id>/query.txt`` (F7), and
another went OUTSIDE its workspace for answer keys — a sibling ws's tasks.json, a held-out
test.json, the bench generator gen.py (F8) — and brute-forced its format rules against the
sealed VAL ``expected_sha256`` values (F9).

Every maintainer/proposer session is scanned after the iteration (harness.evolve):
- F7 ``read eval data``: a tool call or result touching ``<ws>/eval/**`` or a held-out task id;
- F8 ``out-of-workspace access``: a TYPED path (tool arguments, terminal commands, written file
  content) resolving outside ``<ws>`` and outside a small system allowlist;
- F9 ``val-oracle``: a typed item holding a sealed ``expected_sha256`` value of this ws, or
  ``expected_sha256`` together with hashing.
Any hit makes the iteration CONTAMINATED: proposal invalid, no gate run, skills rolled back,
role run dirs redacted, the maintainer's wiki update discarded+purged when it offended.

``python -m hermes_wikiskill.heldout <ws> [--json]`` re-runs the three detectors over every
``runs/maintain-NN`` / ``runs/propose-NN`` session of a ws, read-only (retro-audit).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile

from . import sessions
from .rollout import SKILL_TOOLS, _inside
from .util import read_json

ALLOW_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/proc/self")
ALLOW_FILES = ("/proc/cpuinfo", "/proc/meminfo", "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
               "/dev/tty", "/dev/urandom", "/dev/random")
HASHING = ("sha256(", "hashlib", "sha256sum")
_STOP = "\\s\"'`;|&<>(){}\\[\\],=*?!\\\\"  # T9b: a backslash ends a path (`/dev/null\` continuation, `\n` escapes)
_ABS = re.compile(r"(?<![A-Za-z0-9_.:/~$}\\-])(/[A-Za-z0-9._-][^" + _STOP + r"]*)")
_TILDE = re.compile(r"(?<![A-Za-z0-9_./-])(~(?:/[^" + _STOP + r"]*)?)(?=$|[" + _STOP + r"])")
_HOMEVAR = re.compile(r"\$(?:\{HOME\}|HOME(?![A-Za-z0-9_]))(/[^" + _STOP + r"]*)?")
_REL = re.compile(r"(?<![A-Za-z0-9_./~$:-])((?:\.\.?/|[A-Za-z0-9_][A-Za-z0-9._-]*/)[^" + _STOP + r"]*)")
_SEPS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "\n"}

EVAL_DIR = "eval"
_TOKEN = re.compile(r"[^\s\"'`;|&<>(){}=,:]+")


def eval_root(ws: str) -> str:
    return os.path.join(ws, EVAL_DIR)


def heldout_ids(ws: str) -> set[str]:
    """Task ids of every eval/compare run recorded under ``<ws>/eval`` (summaries + run dirs)."""
    ids: set[str] = set()
    root = eval_root(ws)
    if not os.path.isdir(root):
        return ids
    for tag in sorted(os.listdir(root)):
        tdir = os.path.join(root, tag)
        summary = read_json(os.path.join(tdir, "summary.json"), {})
        for rep in (summary.get("repeats") or []) if isinstance(summary, dict) else []:
            for r in rep.get("results") or []:
                if isinstance(r, dict) and r.get("task_id"):
                    ids.add(str(r["task_id"]))
        runs = os.path.join(tdir, "runs")
        if os.path.isdir(runs):
            for rep in os.listdir(runs):
                rdir = os.path.join(runs, rep)
                if os.path.isdir(rdir):
                    ids.update(n for n in os.listdir(rdir) if os.path.isdir(os.path.join(rdir, n)))
    return ids


def _touches_eval(text: str, ws: str, bases: list[str], *, root_itself: bool) -> bool:
    """A path into ``<ws>/eval`` (``root_itself``: the dir itself counts too — for typed
    arguments; a RESULT that merely lists the ws, e.g. ``ls``, shows only the name)."""
    roots = {os.path.abspath(eval_root(ws)), os.path.join(os.path.realpath(ws), EVAL_DIR)}
    for r in roots:
        if r + os.sep in text:
            return True
        if root_itself and re.search(re.escape(r) + r"(?=$|[\s\"'`,;)\\])", text):
            return True
    for tok in _TOKEN.findall(text):
        if "://" in tok or tok.startswith("~"):
            continue
        if "/" not in tok and not (root_itself and tok == EVAL_DIR):
            continue
        for base in bases:
            p = os.path.normpath(tok if os.path.isabs(tok) else os.path.join(base, tok))
            for r in roots:
                if (root_itself and _inside(p, r)) or os.path.realpath(p).startswith(os.path.realpath(r) + os.sep):
                    return True
    return False


def _args_touch_eval(args: dict, ws: str, bases: list[str]) -> bool:
    """T9f: typed ARGS per key. An absolute ``<ws>/eval`` path counts anywhere (prose too); the
    bare word ``eval`` / relative ``eval/…`` only as a whole path-typed value, a shell word of a
    ``command`` (cwd-tracked) or inside a script-like written file — never in prose/data content."""
    roots = {os.path.abspath(eval_root(ws)), os.path.join(os.path.realpath(ws), EVAL_DIR)}
    prose = _prose_target(args)
    for key, text in _strings(args):
        k = key.lower()
        # absolute <ws>/eval… incl. `<ws>/../ws/eval` (base "/": relative tokens never land in <ws>/eval)
        if _touches_eval(text, ws, [os.sep], root_itself=True):
            return True
        if k in PATH_KEYS:
            cands = [os.path.normpath(os.path.join(b, text.strip())) for b in bases if text.strip()]
        elif k == "command":
            cands = _command_paths(text, ws, ws, bare=(EVAL_DIR,))
        elif not prose:  # a script (or unknown target) that opens eval/… is access
            cands = [os.path.normpath(os.path.join(ws, m.group(1))) for m in _REL.finditer(text)
                     if "://" not in text[m.start():m.end() + 3]]
        else:
            cands = []
        if any(_inside(c, r) for c in cands for r in roots):
            return True
    return False


def heldout_present(ws: str) -> bool:
    """T9g: held-out data exists in the ws (``<ws>/eval`` holds at least one entry)."""
    root = eval_root(ws)
    try:
        return os.path.isdir(root) and bool(os.listdir(root))
    except OSError:
        return False


def detect_access(session: dict, ws: str, ids: set[str] | None = None, *,
                  cwd: str | None = None, present: bool | None = None) -> list[str]:
    """Maintainer/proposer tool calls/results that touch held-out data. Excerpts are
    digests — the record itself must never carry test content into the workspace.

    ``present`` (T9g): whether held-out data existed in the ws when the role STARTED (recorded in
    its run.json). False → nothing to read or leak → no F7 hits. None (legacy runs) → rules on."""
    if present is False:
        return []
    ids = heldout_ids(ws) if ids is None else ids
    bases = [os.path.abspath(ws)] + ([os.path.abspath(cwd)] if cwd else [])
    id_re = (re.compile(r"(?<![A-Za-z0-9_.-])(" + "|".join(map(re.escape, sorted(ids, key=len, reverse=True)))
                        + r")(?![A-Za-z0-9_-])") if ids else None)
    items = [(n, "args", a) for n, a in sessions.tool_calls(session)]
    items += [(n, "result", t) for n, t in sessions.tool_results(session) if n not in SKILL_TOOLS]
    hits = []
    for name, kind, obj in items:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        why = []
        if kind == "args" and isinstance(obj, dict):
            eval_hit = _args_touch_eval(obj, ws, bases)
        else:
            eval_hit = _touches_eval(text, ws, bases, root_itself=kind == "args")
        if eval_hit:
            why.append("eval path")
        if id_re and id_re.search(text):
            why.append("held-out task id")
        if why:
            where = "" if kind == "args" else " result"
            hits.append(f"{name}{where} [{', '.join(why)}]: {sessions.digest(text)}")
    return hits





# ---------------------------------------------------------------- F8: confinement
def _real(p: str) -> str:
    return os.path.realpath(p)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _tmp_roots() -> set[str]:
    out = set()
    for t in ("/tmp", tempfile.gettempdir()):
        out.update({os.path.normpath(t), _real(t)})
    return out


def allowed_outside(path: str, ws: str) -> bool:
    """System paths a role may type (interpreters, /dev/null, scratch files in /tmp) —
    never /tmp/hws-*, the rollout root, $HWS_HOME or a sibling of the workspace."""
    from .rollout import hws_home

    rp = _real(path)
    if any(_under(rp, r) for a in ALLOW_ROOTS for r in {a, _real(a)}):
        return True
    if any(rp in {f, _real(f)} for f in ALLOW_FILES):
        return True
    tmps = _tmp_roots()
    if not any(_under(rp, t) and rp != t for t in tmps):
        return False
    forbidden = {_real(hws_home())}
    if os.environ.get("HWS_ROLLOUT_ROOT"):
        forbidden.add(_real(os.path.expanduser(os.environ["HWS_ROLLOUT_ROOT"])))
    forbidden.add(_real("/tmp/hws-rollouts"))
    parent = _real(os.path.dirname(_real(ws)))
    if parent not in tmps:
        forbidden.add(parent)  # other workspaces next to this one
    if any(_under(rp, f) for f in forbidden):
        return False
    for t in tmps:
        if _under(rp, t) and rp != t and os.path.relpath(rp, t).split(os.sep)[0].startswith("hws-"):
            return False
    return True


def _strings(obj: object) -> list[tuple[str, str]]:
    """(key, text) for every string inside tool arguments (content of written files included)."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, str):
        out.append(("", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out += [(str(k) if not sk else sk, t) for sk, t in _strings(v)]
    elif isinstance(obj, list):
        for v in obj:
            out += _strings(v)
    return out


_PWD_SUBST = ((re.compile(r"\$\(\s*pwd\s*\)|`\s*pwd\s*`"), "$PWD"),
              (re.compile(r"\$\(\s*dirname\s+[\"']?\$\{?PWD\}?[\"']?\s*\)|`\s*dirname\s+[\"']?\$\{?PWD\}?[\"']?\s*`"),
               "$PWD/.."))
_PWDVAR = re.compile(r"\$\{?(OLD)?PWD\}?(?![A-Za-z0-9_])")
# argument KEYS whose whole value is a path (Hermes file tools: path, file_path, …) — R1-03
PATH_KEYS = frozenset({"path", "paths", "dir", "directory", "cwd", "workdir", "working_dir", "root", "file",
                       "file_path", "filepath", "filename", "target_path", "dest", "destination", "src", "source",
                       "folder", "location", "base_dir", "search_path"})
# tools whose RESULT is the content of the file/dir they were pointed at (a typed in-ws path = ws data)
CONTENT_TOOLS = frozenset({"read_file", "search_files", "list_files", "list_directory", "list_dir", "grep",
                           "glob", "file_search", "view_file", "open_file"})


def _pwd(text: str, cwd: str, old: str) -> str:
    for rx, rep in _PWD_SUBST:
        text = rx.sub(rep, text)
    return _PWDVAR.sub(lambda m: old if m.group(1) else cwd, text)


def _command_paths(cmd: str, ws: str, home: str, *, bare: tuple[str, ...] = ()) -> list[str]:
    """Relative paths of a shell command, resolved against the cwd AS THE SHELL TRACKS IT
    (``cd wiki && cat ../x`` = ``<ws>/x``); a bare ``/`` ``//`` ``..`` or ``.`` word is a path;
    ``$PWD``/``$OLDPWD``/``$(pwd)``/``$(dirname "$PWD")`` resolve against the tracked cwd."""
    for rx, rep in _PWD_SUBST:
        cmd = rx.sub(rep, cmd)
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        words = list(lex)
    except ValueError:
        words = cmd.split()
    cwd, old, out, i = ws, ws, [], 0

    def resolve(w: str, base: str) -> str:
        if w.startswith("~"):
            return os.path.normpath(home + w[1:])
        return os.path.normpath(w if os.path.isabs(w) else os.path.join(base, w))

    words = [w.rstrip("\\") or w for w in words]  # T9b: `cmd /dev/null\` line continuation
    while i < len(words):
        w = _pwd(words[i], cwd, old)
        if words[i] == "cd":
            nxt = _pwd(words[i + 1], cwd, old) if i + 1 < len(words) and words[i + 1] not in _SEPS else "~"
            if nxt == "-":
                cwd, old = old, cwd
            elif not nxt.startswith("$"):
                cwd, old = resolve(nxt, cwd), cwd
                out.append(cwd)
            i += 2
            continue
        if w in ("/", "//"):
            out.append("/")
        elif w in ("..", "../", ".", "./") or w.rstrip("/") in bare:  # bare: T9f `ls eval`
            out.append(os.path.normpath(os.path.join(cwd, w)))
        elif w.startswith("/") and words[i].startswith("$"):
            out.append(os.path.normpath(w))  # a $PWD-derived absolute path
        else:
            part = w.split("=", 1)[1] if w.startswith("-") and "=" in w else w
            if part and not part.startswith(("/", "~", "$", "-")) and "://" not in part:
                if re.search(r"[\s'\"()]", part):  # a quoted code snippet: its path-like tokens
                    out += [os.path.normpath(os.path.join(cwd, m.group(1))) for m in _REL.finditer(part)]
                elif "/" in part:
                    out.append(os.path.normpath(os.path.join(cwd, part)))
        i += 1
    return out


def path_candidates(text: str, *, ws: str, home: str, command: bool = False,
                    path_arg: bool = False, relative: bool = True) -> list[str]:
    """Normalised paths a typed text refers to (see QA-F8 rules in the module docstring).
    ``path_arg``: the WHOLE value is a path (a path-typed argument key, R1-03).
    ``relative=False``: content of a prose/data file — relative tokens are quotes, not access (T9e)."""
    out = []
    if path_arg or text.strip() in ("/", "//", "..", ".", "../", "./"):
        v = _pwd(text.strip(), ws, ws)
        if v:
            out.append(os.path.normpath(os.path.join(home, v[2:]) if v.startswith("~/") else
                                        home if v == "~" else os.path.join(ws, v)))
    for m in _ABS.finditer(text):
        tok = m.group(1)
        top = tok.strip("/").split("/")[0]
        if not command and not path_arg and "/" not in tok.strip("/"):
            continue  # T9b: `/tmp`, `/home` in prose/written content access nothing (`ls /home` does)
        if top and os.path.lexists("/" + top):
            out.append(os.path.normpath(tok))
    for m in _TILDE.finditer(text):
        out.append(os.path.normpath(os.path.join(home, m.group(1)[2:]) if m.group(1) != "~" else home))
    for m in _HOMEVAR.finditer(text):
        out.append(os.path.normpath(home + (m.group(1) or "")))
    if command:
        out += _command_paths(text, ws, home)
    elif relative:
        text = _pwd(text, ws, ws)
        for m in _REL.finditer(text):
            if "://" in text[m.start():m.end() + 3]:
                continue
            out.append(os.path.normpath(os.path.join(ws, m.group(1))))
    return out


def _rollout_roots() -> set[str]:
    roots = {"/tmp/hws-rollouts"}
    if os.environ.get("HWS_ROLLOUT_ROOT"):
        roots.add(os.path.normpath(os.path.expanduser(os.environ["HWS_ROLLOUT_ROOT"])))
    return roots | {_real(r) for r in roots}


def _quoted_rollout_path(p: str) -> bool:
    """A task-rollout path (the ws's own traces record them; the dirs are long gone)."""
    rp = _real(p)
    return any(_under(p, r) or _under(rp, r) for r in _rollout_roots())


def _result_allowed(p: str) -> bool:
    """Paths a RESULT may show beyond the typed allowlist: task-rollout paths recorded in
    the ws's own traces, TRUNCATED prefixes of them (`/tmp/hws-roll` from a script printing
    transcripts, T9e) and the hermes binary the framework launches (T9c)."""
    if _real(p) in _hermes_binaries():  # T9c: cmd[0] of the ws's own run.json records
        return True
    if any(r.startswith(p) for r in _rollout_roots()):  # T9e: a truncated rollout path
        return True
    return _quoted_rollout_path(p)


def _hermes_binaries() -> set[str]:
    """The hermes binary the framework launches (compat.resolve_binary: HERMES_BIN or PATH),
    exact path + realpath. RESULTS only — a typed path to it is still out-of-ws access."""
    from . import compat
    from .errors import HermesIncompatibleError

    try:
        b = compat.resolve_binary()
    except HermesIncompatibleError:
        return set()
    return {os.path.normpath(b), _real(b)}


def _result_texts(result: str) -> list[str]:
    """A tool result's strings, JSON-decoded when it is JSON (so ``\\n`` ends a path)."""
    try:
        obj = json.loads(result)
    except ValueError:
        return [result.replace("\\/", "/")]
    return [t for _, t in _strings(obj)]


def _calls_with_results(session: dict) -> list[tuple[str, object, str | None]]:
    """(tool name, parsed args, result text | None) in call order."""
    results: dict[str, str] = {}
    for m in session.get("messages") or []:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id"):
            c = m.get("content")
            results[str(m["tool_call_id"])] = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    out = []
    for m in session.get("messages") or []:
        calls = m.get("tool_calls") if isinstance(m, dict) else None
        if isinstance(calls, str):
            try:
                calls = json.loads(calls)
            except ValueError:
                calls = []
        for c in calls or []:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") if isinstance(c.get("function"), dict) else c
            args = fn.get("arguments", fn.get("args"))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            cid = c.get("id") or c.get("call_id")
            out.append((str(fn.get("name")), args, results.get(str(cid)) if cid else None))
    return out


def detect_outside(session: dict, ws: str, *, home: str | None = None) -> list[str]:
    """F8: TYPED paths (arguments, commands, written content) outside ``<ws>``, plus (R1-03)
    absolute out-of-ws paths in non-skill tool RESULTS. ``home`` resolves ``~``/``$HOME``: the
    role home recorded in run.json for T8+ role runs, else the operator's HOME (legacy runs)."""
    return _outside_scan(session, ws, home)[0]


PATH_MAX = 160
PROSE_EXT = (".md", ".markdown", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".log", ".rst")


def _prose_target(args: object) -> bool:
    """T9e: the call writes/patches a prose or data file (relative tokens in its content are
    quotes). Script-like targets (.py .sh ..., or content starting with ``#!``) are not prose."""
    if not isinstance(args, dict):
        return False
    target = next((args[k] for k in ("path", "file_path", "filepath", "file", "filename", "target_path")
                   if isinstance(args.get(k), str)), None)
    if not target or not target.lower().endswith(PROSE_EXT):
        return False
    body = next((args[k] for k in ("content", "new_string", "text") if isinstance(args.get(k), str)), "")
    return not body.lstrip().startswith("#!")


def outside_paths(session: dict, ws: str, *, home: str | None = None,
                  ids: set[str] | None = None) -> list[dict[str, str]]:
    """T9d: the flagged out-of-ws PATHS in plain text (``{tool, key, kind: arg|result, path}``,
    path <= 160 chars, held-out ids masked) so a voided iteration is diagnosable without a tap."""
    out, seen = [], set()
    for d in _outside_scan(session, ws, home)[1]:
        p = d["path"]
        for i in sorted(ids or (), key=len, reverse=True):
            p = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(i) + r"(?![A-Za-z0-9_-])", "<held-out-id>", p)
        d = {**d, "path": p if len(p) <= PATH_MAX else p[:PATH_MAX - 3] + "..."}
        k = tuple(sorted(d.items()))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _outside_scan(session: dict, ws: str, home: str | None) -> tuple[list[str], list[dict[str, str]]]:
    home = home or os.path.expanduser("~")
    hits: list[str] = []
    details: list[dict[str, str]] = []
    for name, args, result in _calls_with_results(session):
        bad = []
        prose = _prose_target(args)
        for key, text in _strings(args):
            k = key.lower()
            content = k != "command" and k not in PATH_KEYS
            for p in path_candidates(text, ws=ws, home=home, command=(k == "command"), path_arg=k in PATH_KEYS,
                                     relative=not (content and prose)):
                if _inside(p, ws) or allowed_outside(p, ws):
                    continue
                if content and _quoted_rollout_path(p):  # T9e: quoted from a train trace, not access
                    continue
                bad.append(p)
                details.append({"tool": name, "key": key or "(args)", "kind": "arg", "path": p})
        seen = []
        if result and name not in SKILL_TOOLS and not (name in CONTENT_TOOLS and not bad):
            for m in (m for t in _result_texts(result) for m in _ABS.finditer(t)):
                tok = os.path.normpath(m.group(1))
                top = tok.strip("/").split("/")[0]
                # a bare top-level dir (`/tmp`, quoted from a train trace) reveals no file; a
                # search/listing outside the ws shows deeper paths (/home/u/ops/.../test.json)
                if not top or tok.count("/") < 2 or not os.path.lexists("/" + top):
                    continue
                if _inside(tok, ws) or allowed_outside(tok, ws) or _result_allowed(tok):
                    continue
                seen.append(tok)
                details.append({"tool": name, "key": "result", "kind": "result", "path": tok})
        if bad or seen:
            text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, sort_keys=True)
            what = []
            if bad:
                what.append(f"outside ws x{len(bad)} {sessions.digest(sorted(set(bad)))}")
            if seen:
                what.append(f"result shows outside x{len(seen)} {sessions.digest(sorted(set(seen)))}")
            hits.append(f"{name} [{'; '.join(what)}]: {sessions.digest(text)}")
    return hits, details


# ---------------------------------------------------------------- F9: val-hash oracle
def sealed_digests(ws: str) -> set[str]:
    """``expected_sha256`` values of the sealed tasks in ``<ws>/tasks.json``."""
    data = read_json(os.path.join(ws, "tasks.json"), [])
    tasks = data.get("tasks", []) if isinstance(data, dict) else data
    out = set()
    for t in tasks if isinstance(tasks, list) else []:
        g = t.get("grader") if isinstance(t, dict) else None
        if isinstance(g, dict) and isinstance(g.get("expected_sha256"), str):
            out.add(g["expected_sha256"].lower())
    return out


def detect_val_oracle(session: dict, ws: str, digests: set[str] | None = None) -> list[str]:
    digests = sealed_digests(ws) if digests is None else digests
    hits = []
    for name, args in sessions.tool_calls(session):
        text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, sort_keys=True)
        low = text.lower()
        why = []
        if any(d in low for d in digests):
            why.append("sealed digest")
        if "expected_sha256" in text and any(h in text for h in HASHING):
            why.append("expected_sha256 + hashing")
        if why:
            hits.append(f"{name} [{', '.join(why)}]: {sessions.digest(text)}")
    return hits


# ---------------------------------------------------------------- all rules
CATEGORIES = (("eval", "read eval data"), ("outside", "out-of-workspace access"), ("oracle", "val-oracle"))


def audit_session(session: dict, ws: str, *, ids: set[str] | None = None,
                  digests: set[str] | None = None, home: str | None = None,
                  present: bool | None = None) -> dict[str, list[str]]:
    ids = heldout_ids(ws) if ids is None else ids
    hits = {"eval": detect_access(session, ws, ids, present=present), "outside": detect_outside(session, ws, home=home),
            "oracle": detect_val_oracle(session, ws, digests)}
    if hits["outside"]:  # T9d: plain-text paths of out-of-ws hits only (never eval/held-out hits)
        hits["outside_paths"] = outside_paths(session, ws, home=home, ids=ids)
    return hits


def path_digests(paths: list[dict[str, str]]) -> list[dict[str, str]]:
    """T10g (QA-F27): the PATH-FREE form of flagged out-of-ws paths for role-readable ws records
    (``{tool, key, kind, digest}``, like F7 hits) — a later role reading/echoing the record must not
    hit F8 on the framework's own bookkeeping. Plain paths stay out of the ws (operator diagnostic)."""
    return [{"tool": d.get("tool", ""), "key": d.get("key", ""), "kind": d.get("kind", ""),
             "digest": d["digest"] if isinstance(d.get("digest"), str) else sessions.digest(str(d.get("path", "")))}
            for d in paths if isinstance(d, dict)]


def path_summary(paths: list[dict[str, str]], n: int = 2, width: int = 100) -> str:
    """``sha256:… (tool.key arg), ...`` for the first ``n`` flagged paths — digests only, never the
    path (T9d history note; T10g: the note lands in wiki/log.md, skill-impact.md and state history)."""
    parts = [f"{d['digest'][:width]} ({d['tool']}.{d['key']} {d['kind']})" for d in path_digests(paths[:n])]
    more = f" +{len(paths) - n} more" if len(paths) > n else ""
    return ", ".join(parts) + more


def contamination_note(found: dict[str, dict[str, list[str]]]) -> str | None:
    """``{role: {category: hits}}`` → ``contaminated: <kind> (<role> xN; <digest>)`` parts, or None."""
    parts = []
    for cat, label in CATEGORIES:
        roles = [(r, h[cat]) for r, h in found.items() if h.get(cat)]
        if roles:
            first = roles[0][1][0]
            paths = [d for r, h in found.items() for d in (h.get("outside_paths") or [])] if cat == "outside" else []
            where = f"paths: {path_summary(paths)}; " if paths else ""
            parts.append(f"contaminated: {label} ({', '.join(f'{r} x{len(h)}' for r, h in roles)}; {where}{first[:120]})")
    return " | ".join(parts) or None


def _role_runs(ws: str) -> list[tuple[int, str, str]]:
    """Every role run: ``runs/<role>-NN`` (final attempt, key ``maintainer``) and each kept
    earlier attempt ``runs/<role>-NN/attempt-<k>`` (key ``maintainer/attempt-<k>``, R1-01)."""
    runs = os.path.join(ws, "runs")
    out = []
    for n in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        m = re.fullmatch(r"(maintain|propose)-(\d+)", n)
        if m:
            role = "maintainer" if m.group(1) == "maintain" else "proposer"
            rdir = os.path.join(runs, n)
            out.append((int(m.group(2)), role, rdir))
            att = [a for a in os.listdir(rdir) if re.fullmatch(r"attempt-\d+", a)
                   and os.path.isdir(os.path.join(rdir, a))]
            for a in sorted(att, key=lambda a: int(a.split("-")[1])):
                out.append((int(m.group(2)), f"{role}/{a}", os.path.join(rdir, a)))
    return out


_NOT_LAUNDERING = re.compile(r"runs/proposals/iter-\d+\.json|runs/state\.json|\.hws\.lock")


def _quarantine_removed(rec: object) -> list[str]:
    """T11 (R3-01): files a quarantine REMOVED after this (bad) role run — role output. Created dirs,
    the proposal file, runs/state.json and the lock are harness paths, never evidence."""
    q = rec.get("quarantine") if isinstance(rec, dict) else None
    removed = q.get("removed") if isinstance(q, dict) else None
    return [p for p in removed or [] if isinstance(p, str) and p and not p.endswith(os.sep)
            and not _NOT_LAUNDERING.fullmatch(p.replace(os.sep, "/"))]


def _arg_strings(a: object, key: str = "") -> list[tuple[str, str]]:
    """Every string value of a tool call's args as (key, value); JSON-string args are decoded."""
    if isinstance(a, str) and not key:
        try:
            a = json.loads(a)
        except ValueError:
            return [("", a)]
    if isinstance(a, dict):
        return [x for k, v in a.items() for x in _arg_strings(v, str(k))]
    if isinstance(a, list):
        return [x for v in a for x in _arg_strings(v, key)]
    return [(key, a)] if isinstance(a, str) else []


def _path_tokens(name: str, args: object, ws_roots: tuple[str, ...]) -> set[str]:
    """QA-F30: the ws-relative PATHS a tool call names — each string arg as one token (a terminal
    command split with shlex, whitespace fallback), quotes stripped, normpath'd, a leading ./ dropped,
    an absolute path under the ws mapped to its relpath. Paths outside the ws are dropped."""
    out = set()
    for key, v in _arg_strings(args):
        if name == "terminal" or key == "command":
            try:
                parts = shlex.split(v)
            except ValueError:
                parts = v.split()
        else:
            parts = [v]
        for t in parts:
            t = t.strip().strip("'\"`").strip()
            if not t:
                continue
            if os.path.isabs(t):
                t = os.path.normpath(t)
                rel = next((os.path.relpath(t, r) for r in ws_roots if t.startswith(r + os.sep)), None)
                if rel is None:
                    continue
                t = rel
            t = os.path.normpath(t)
            if t not in (".", "") and not t.startswith(".." + os.sep) and t != "..":
                out.add(t)
    return out


def _laundering(ws: str, order: list[tuple], iters: dict[int, dict]) -> None:
    """T11 (probe c07): a role run whose tool calls name a file that the quarantine of an EARLIER
    role run removed tried to launder that voided run's output (records only, read-only).
    QA-F30: a path counts only when a path TOKEN of a call equals it exactly (precision over recall:
    the quarantine already removed the file); a path the run write_file'd itself first is its own."""
    roots = tuple(dict.fromkeys((os.path.normpath(os.path.abspath(ws)), os.path.realpath(ws))))
    order.sort(key=lambda o: (o[0], o[1], o[2]))
    removed: set[str] = set()
    for it, _ns, _seq, role, sess, rm in order:
        if removed:
            hits, written = set(), set()
            for name, a in sessions.tool_calls(sess):
                named = _path_tokens(name, a, roots) & removed
                if name == "write_file":
                    written |= named
                else:
                    hits |= named - written
            if hits:
                iters[it]["roles"][role]["quarantined_reads"] = sorted(hits)[:20]
        removed |= {os.path.normpath(p) for p in rm}


def retro_audit(ws: str, *, home: str | None = None) -> dict:
    """Read-only per-iteration verdicts for every role session of ``ws``."""
    ids, digests = heldout_ids(ws), sealed_digests(ws)
    # R2-04: the harness state lives at runs/state.json (<ws>/state.json only for legacy layouts)
    state = read_json(os.path.join(ws, "runs", "state.json"), None) or read_json(os.path.join(ws, "state.json"), {})
    hist = {h.get("iter"): h for h in (state.get("history") or [])} if isinstance(state, dict) else {}
    iters: dict[int, dict] = {}
    order: list[tuple] = []  # T11: (iter, started_ns, seq, role key, session, quarantine-removed paths)
    for it, role, rdir in _role_runs(ws):
        sf = os.path.join(rdir, "session.jsonl")
        sess = sessions.load(sf) if os.path.isfile(sf) else {}
        rec = read_json(os.path.join(rdir, "run.json"), {})
        role_home = rec.get("role_home") if isinstance(rec, dict) else None
        present = rec.get("heldout_present") if isinstance(rec, dict) else None  # T9g; None = legacy
        entry = iters.setdefault(it, {"iter": it, "roles": {}})
        info = {"session": os.path.relpath(sf, ws) if os.path.isfile(sf) else None,
                "tool_calls": len(sessions.tool_calls(sess)), "redacted": bool(sess.get("redacted")),
                "home": "role" if role_home else "operator", "heldout_present": present}
        # `~` = the recorded role home (T8+); legacy pre-T8 runs had the operator's HOME
        info.update(audit_session(sess, ws, ids=ids, digests=digests, home=role_home or home,
                                  present=present if isinstance(present, bool) else None))
        # R2-04: a voided run's session is redacted — its RECORDED verdict (confinement.json) counts
        conf = read_json(os.path.join(rdir, "confinement.json"), None)
        if isinstance(conf, dict):
            info["confinement"] = conf.get("counts") or {}
            rel = os.path.relpath(os.path.join(rdir, "confinement.json"), ws)
            for c, _ in CATEGORIES:
                n = int((conf.get("counts") or {}).get(c) or 0)
                if n and not info.get(c):
                    info[c] = [f"recorded in {rel}"] * n
            if conf.get("outside") and not info.get("outside_paths"):
                info["outside_paths"] = conf["outside"]
        entry["roles"][role] = info
        order.append((it, rec.get("started_ns") if isinstance(rec, dict) and isinstance(rec.get("started_ns"), int)
                      else -1, len(order), role, sess, _quarantine_removed(rec)))
    _laundering(ws, order, iters)
    for it, entry in iters.items():
        found = {r: {c: i.get(c) or [] for c in [c for c, _ in CATEGORIES] + ["outside_paths"]}
                 for r, i in entry["roles"].items()}
        note = contamination_note(found)
        recorded = (hist.get(it) or {}).get("proposal")
        laundered = [f"{r}: {', '.join(i['quarantined_reads'][:3])}" for r, i in entry["roles"].items()
                     if i.get("quarantined_reads")]
        entry["verdict"] = ("contaminated" if note else "laundering-attempt" if laundered else
                            "redacted" if any(i["redacted"] for i in entry["roles"].values()) else "clean")
        if not note and laundered:
            note = ("laundering attempt: read output a voided earlier role run left and the harness "
                    f"quarantined ({'; '.join(laundered)})")[:400]
        entry["note"] = note
        entry["recorded"] = recorded
    return {"ws": os.path.abspath(ws), "heldout_ids": len(ids), "sealed_digests": len(digests),
            "iterations": [iters[k] for k in sorted(iters)]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hermes_wikiskill.heldout",
                                 description="read-only retro-audit of maintainer/proposer sessions (QA-F7/F8/F9)")
    ap.add_argument("ws")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.ws):
        print(f"not a workspace: {a.ws}", file=sys.stderr)
        return 2
    r = retro_audit(a.ws)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        for it in r["iterations"]:
            calls = ", ".join(f"{k} {v['tool_calls']} calls" for k, v in it["roles"].items())
            print(f"iter {it['iter']:02d}: {it['verdict'].upper()} ({calls})" + (f" - {it['note']}" if it["note"] else ""))
    return 1 if any(i["verdict"] in ("contaminated", "laundering-attempt") for i in r["iterations"]) else 0


__all__ = ["heldout_ids", "heldout_present", "detect_access", "eval_root", "detect_outside", "detect_val_oracle",
           "audit_session", "contamination_note", "retro_audit", "sealed_digests", "allowed_outside"]


if __name__ == "__main__":
    raise SystemExit(main())
