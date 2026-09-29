"""hermes-wikiskill (alias hws) — drive WikiSkill evolution workspaces on Hermes.

Global options go before or after the verb: --ws-root DIR, --hermes BIN,
--source-profile NAME, --json. Machine output (--json) is exactly one JSON
document on stdout; human output goes through fmt.render.
Exit codes: 0 ok, 1 failure, 2 usage, 3 skills invisible, 4 Hermes incompatible,
75 workspace busy.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import sys

from . import __version__, compat, gating, harness, tasks as tasks_mod, traces, transfer
from .backends import hermes
from .errors import HermesIncompatibleError, HwsError, UsageError
from .locking import is_locked, workspace_lock
from .resources import DEFAULT_BENCH, bench_dir


def hws_home() -> str:
    return os.path.expanduser(os.environ.get("HWS_HOME") or "~/ops/hermes-wikiskill")


def default_ws_root() -> str:
    return os.path.join(hws_home(), "workspaces")


def resolve_ws(args) -> str:
    if getattr(args, "ws_path", None):
        return os.path.abspath(os.path.expanduser(args.ws_path))
    name = args.ws
    if os.sep in name or name.startswith((".", "~")):
        return os.path.abspath(os.path.expanduser(name))
    root = args.ws_root or default_ws_root()
    return os.path.abspath(os.path.join(os.path.expanduser(root), name))


def _existing_ws(args) -> str:
    ws = resolve_ws(args)
    if not os.path.exists(tasks_mod.tasks_path(ws)):
        raise HwsError(f"no workspace at {ws} (run `hermes-wikiskill init`)")
    return ws


@contextlib.contextmanager
def _locked(ws: str, args, *, dry_run: bool = False):
    if dry_run:
        yield None
    else:
        with workspace_lock(ws, wait=getattr(args, "wait", False)) as p:
            yield p


def _check_parallel(args) -> None:
    if getattr(args, "max_parallel", 1) != 1:
        raise UsageError("--max-parallel > 1 is not supported (2-vCPU/2 GB host: one rollout at a time)")


def _tasks_source(args) -> tuple[str, list[dict]]:
    if getattr(args, "tasks", None):
        path = os.path.abspath(os.path.expanduser(args.tasks))
    else:
        d = bench_dir(getattr(args, "bench", None) or DEFAULT_BENCH)
        if not d:
            raise UsageError(f"bundled bench {args.bench or DEFAULT_BENCH!r} not found; pass --tasks FILE")
        path = os.path.join(d, "tasks.json")
    return path, tasks_mod.load_file(path, allowed_splits=tasks_mod.WORKSPACE_SPLITS)


# ------------------------------------------------------------------ verbs

def cmd_version(args):
    try:
        hv = compat.hermes_version(compat.resolve_binary(args.hermes))
    except HermesIncompatibleError:
        hv = None
    return "version", {"tool": "hermes-wikiskill", "version": __version__, "hermes_version": hv,
                       "python": platform.python_version()}


def cmd_init(args):
    ws = resolve_ws(args)
    if os.path.exists(tasks_mod.tasks_path(ws)):
        raise HwsError(f"workspace already exists: {ws}")
    src, tasks = _tasks_source(args)
    if args.dry_run:
        return "init", {**harness.init_workspace(ws, tasks, dry_run=True), "tasks_file": src}
    with _locked(ws, args):
        out = harness.init_workspace(ws, tasks, source_profile=args.source_profile,
                                     model=args.model, provider=args.provider)
    return "init", {**out, "tasks_file": src}


def cmd_bench(args):
    ws = _existing_ws(args)
    src, tasks = _tasks_source(args)
    if args.dry_run:
        return "bench", {"dry_run": True, "ws": ws, "tasks_file": src, "tasks": len(tasks)}
    with _locked(ws, args):
        out = tasks_mod.replace_tasks(ws, tasks)
    return "bench", {"dry_run": False, "ws": ws, "tasks_file": src, **out}


def usage_totals(ws: str) -> dict:
    """Cumulative recorded cost of every rollout of a workspace (unknown costs counted, not guessed)."""
    import glob

    by: dict[str, dict] = {k: {"cost_usd": 0.0, "runs": 0, "runs_without_cost": 0}
                           for k in ("traces", "runs", "eval")}

    def add(src: str, usage) -> None:
        c = (usage or {}).get("cost_usd") if isinstance(usage, dict) else None
        by[src]["runs"] += 1
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            by[src]["cost_usd"] += c
        else:
            by[src]["runs_without_cost"] += 1

    for t in traces.list_traces(ws):
        add("traces", t["meta"].get("usage"))
    for p in sorted(glob.glob(os.path.join(ws, "runs", "*", "run.json"))):
        if os.path.basename(os.path.dirname(p)).startswith(("maintain-", "propose-")):
            add("runs", hermes.load_run_record(os.path.dirname(p)).get("usage"))
    for p in sorted(glob.glob(os.path.join(ws, "eval", "*", "summary.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                summary = json.load(f)
        except (OSError, ValueError):
            continue
        for rep in summary.get("repeats", []):
            for r in rep.get("results", []):
                add("eval", r.get("usage"))
    for v in by.values():
        v["cost_usd"] = round(v["cost_usd"], 6)
    total = round(sum(v["cost_usd"] for v in by.values()), 6)
    runs = sum(v["runs"] for v in by.values())
    without = sum(v["runs_without_cost"] for v in by.values())
    return {"cost_usd": total, "runs": runs, "runs_with_cost": runs - without,
            "runs_without_cost": without, "by_source": by}


def status_data(ws: str) -> dict:
    sp = tasks_mod.splits(ws)
    state = gating.load_state(ws)
    pdir = os.path.join(ws, "wiki", "patterns")
    return {"ws": ws, "domain": state.get("domain"),
            "tasks": {"train": len(sp["train"]), "val": len(sp["val"])},
            "baseline": state.get("baseline"), "r_best": state.get("r_best"),
            "next_iter": state.get("next_iter"), "history": state.get("history", []),
            "active_skills": transfer.active_skills(ws),
            "wiki_patterns": len(os.listdir(pdir)) if os.path.isdir(pdir) else 0,
            "traces": len(traces.list_traces(ws)), "usage": usage_totals(ws),
            "settings": {k: v for k, v in hermes.ws_settings(ws).items()},
            "locked": is_locked(ws)}


def cmd_status(args):
    return "status", status_data(_existing_ws(args))


def cmd_evolve(args):
    ws = _existing_ws(args)
    _check_parallel(args)
    with _locked(ws, args, dry_run=args.dry_run):
        state = harness.evolve(ws, iters=args.iters, model=args.model, provider=args.provider,
                               dry_run=args.dry_run, verbose=not args.json,
                               max_turns=args.max_turns, no_early_stop=args.no_early_stop)
    return "evolve", {"ws": ws, **state}


def cmd_gate(args):
    ws = _existing_ws(args)
    _check_parallel(args)
    tasks = tasks_mod.splits(ws)[args.split]
    with _locked(ws, args, dry_run=args.dry_run):
        if not args.dry_run:
            gating.guard_gated_tree(ws, "gate")  # R2-05
        g = gating.run_gate(ws, tasks, args.iter, model=args.model, dry_run=args.dry_run,
                            overwrite=args.overwrite, runner_kwargs={"max_turns": args.max_turns})
    return "gate", {"ws": ws, "split": args.split, "iter": args.iter, "mean": g["mean"],
                    "dry_run": args.dry_run,
                    "results": [{"task_id": r["id"], "score": r["score"],
                                 **({"cmd": r["result"].get("cmd")} if args.dry_run else {})}
                                for r in g["results"]]}


def cmd_run_task(args):
    ws = _existing_ws(args)
    t = next((t for t in tasks_mod.load(ws) if t["id"] == args.task_id), None)
    if not t:
        raise UsageError(f"unknown task: {args.task_id}")
    with _locked(ws, args, dry_run=args.dry_run):
        r = gating.run_task(ws, t, args.iter, model=args.model, dry_run=args.dry_run,
                            overwrite=args.overwrite,
                            runner_kwargs={"max_turns": args.max_turns, "run_budget": args.run_budget})
    res = r["result"]
    return "run-task", {"ws": ws, "task_id": t["id"], "iter": args.iter, "score": r["score"],
                        "dry_run": args.dry_run, "cmd": res.get("cmd"),
                        "skills_expected": res.get("skills_expected"),
                        "skills_visible": res.get("skills_visible"),
                        "skill_views": res.get("skill_views"), "usage": res.get("usage"),
                        "exit_code": res.get("exit_code")}


def cmd_maintain(args):
    ws = _existing_ws(args)
    tr = traces.list_traces(ws, it=args.iter, split="train")
    sampled = [{"task_id": t["task_id"], "score": t["meta"].get("score"),
                "title": t["meta"].get("title", ""), "feedback": t["meta"].get("feedback"),
                "path": traces.transcript_path(ws, args.iter, "train", t["task_id"])
                if os.path.exists(traces.transcript_path(ws, args.iter, "train", t["task_id"]))
                else t["path"]} for t in tr]
    note = None
    with _locked(ws, args, dry_run=args.dry_run):
        if args.dry_run:
            res = harness.maintain_step(ws, args.iter, sampled, dry_run=True)
        else:  # R1-01: audited like evolve; a bad run's wiki changes are discarded, rc 1
            res, note = harness.maintain_audited(ws, args.iter, sampled)
    return "maintain", {"ws": ws, "iter": args.iter, "traces": len(sampled),
                        "dry_run": args.dry_run, "cmd": res.get("cmd"),
                        "discarded": note}, (1 if note else 0)


def cmd_propose(args):
    ws = _existing_ws(args)
    tr = [{"id": t["task_id"], "split": "train", "title": t["meta"].get("title", ""),
           "score": t["meta"].get("score"), "feedback": t["meta"].get("feedback")}
          for t in traces.list_traces(ws, it=args.iter, split="train")]
    with _locked(ws, args, dry_run=args.dry_run):
        if args.dry_run:
            proposal, res, note = harness.propose_step(ws, args.iter, tr, dry_run=True)
        else:  # R1-01: audited like evolve; contaminated → invalid proposal, rc 1
            proposal, res, note = harness.propose_audited(ws, args.iter, tr)
    return "propose", {"ws": ws, "iter": args.iter, "dry_run": args.dry_run, "cmd": res.get("cmd"),
                       "proposal": None if not proposal else
                       {k: proposal.get(k) for k in ("action", "name")}, "invalid": note}, (1 if note else 0)


def cmd_reset(args):
    ws = _existing_ws(args)
    plan = {"ws": ws, "removes": ["raw/", "runs/"], "skills": "reset to last commit",
            "dry_run": args.dry_run}
    if args.dry_run:
        return "reset", plan
    with _locked(ws, args):
        state = gating.load_state(ws)
        if state.get("role_running"):  # T11 m2: a hard-killed role — quarantine it (or fail closed) first,
            harness._recover_role(ws, state)  # which also removes its snapshot; HwsError propagates
        for d in ("raw", "runs"):
            shutil.rmtree(os.path.join(ws, d), ignore_errors=True)
        os.makedirs(os.path.join(ws, "runs", "proposals"), exist_ok=True)
        gating.rollback(ws)
    return "reset", plan


def cmd_eval(args):
    from .evaluate import evaluate
    ws = _existing_ws(args)
    _check_parallel(args)
    with _locked(ws, args, dry_run=args.dry_run):
        out = evaluate(ws, os.path.abspath(os.path.expanduser(args.tasks)), skills=args.skills,
                       repeat=args.repeat, tag=args.tag, max_turns=args.max_turns,
                       run_budget=args.run_budget, model=args.model, dry_run=args.dry_run,
                       overwrite=args.overwrite, verbose=not args.json)
    return "eval", out


def _ws_arg(root: str, name: str) -> str:
    return name if os.path.isdir(name) else os.path.join(root, name)


def cmd_compare(args):
    from . import compare
    root = os.path.expanduser(args.ws_root or default_ws_root())
    a, b = _ws_arg(root, args.ws_a), _ws_arg(root, args.ws_b)
    for p in (a, b):
        if not os.path.exists(tasks_mod.tasks_path(p)):
            raise HwsError(f"workspace not found: {p}")
    with _locked(a, args, dry_run=args.dry_run), _locked(b, args, dry_run=args.dry_run):
        rep = compare.run_comparison(a, b, iters=args.iters, dry_run=args.dry_run,
                                     max_turns=args.max_turns)
    if not args.json:
        print(compare.format_report(rep))
        return None, None
    return "compare", rep


def cmd_transfer(args):
    root = os.path.expanduser(args.ws_root or default_ws_root())
    src, dst = _ws_arg(root, args.src), _ws_arg(root, args.dst)
    with _locked(dst, args, dry_run=args.dry_run):
        m = transfer.transfer_skills(src, dst, force=args.force, dry_run=args.dry_run)
    if not args.json:
        print(transfer.format_report(m))
        return None, None
    return "transfer", m


def cmd_report(args):
    from . import report
    ws = resolve_ws(args)
    doctor = None
    if args.doctor_json:
        with open(os.path.expanduser(args.doctor_json), encoding="utf-8") as f:
            doctor = json.load(f)
    else:
        try:
            from . import doctor as doctor_mod  # TASK 3
            doctor = doctor_mod.run_checks(profile=getattr(args, "profile", None), hermes=args.hermes,
                                           ws_root=args.ws_root)
        except ImportError:
            doctor = None
    data = report.build(ws, acceptance_dir=args.acceptance_dir, doctor=doctor)
    if args.html:
        from .util import atomic_write_text
        atomic_write_text(os.path.abspath(os.path.expanduser(args.html)), report.render_html(data))
    return "report", data


# ------------------------------------------------------------------ parser

def _globals(p: argparse.ArgumentParser, suppress: bool) -> None:
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    p.add_argument("--ws-root", default=d(None), help="workspace root (default $HWS_HOME/workspaces)")
    p.add_argument("--hermes", default=d(None), help="hermes binary (default $HERMES_BIN or PATH)")
    p.add_argument("--source-profile", default=d(None),
                   help="Hermes profile credentials are read from (default: default)")
    p.add_argument("--json", action="store_true", default=d(False), help="one JSON document on stdout")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes-wikiskill", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    _globals(p, suppress=False)
    sub = p.add_subparsers(dest="cmd", required=True, metavar="VERB")

    def verb(name, fn, help_, ws=True, dry=True, lock=True):
        sp = sub.add_parser(name, help=help_)
        _globals(sp, suppress=True)
        if ws:
            sp.add_argument("ws", help="workspace name (under --ws-root) or path")
            sp.add_argument("--ws", dest="ws_path", help="explicit workspace path (upstream compat)")
        if dry:
            sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
        if lock:
            sp.add_argument("--wait", action="store_true", help="block on a busy workspace lock")
        sp.set_defaults(fn=fn)
        return sp

    verb("version", cmd_version, "tool, Hermes and Python versions", ws=False, dry=False, lock=False)

    sp = verb("init", cmd_init, "create a workspace from a train/val tasks file")
    sp.add_argument("--tasks", help="tasks.json with train/val tasks (default: bundled house-report)")
    sp.add_argument("--bench", help=f"bundled bench name (default {DEFAULT_BENCH})")
    sp.add_argument("--model", help="model id for the workspace profile")
    sp.add_argument("--provider", help="provider for --model")

    sp = verb("bench", cmd_bench, "replace the workspace tasks (re-materializes every sandbox)")
    sp.add_argument("--tasks")
    sp.add_argument("--bench")
    sp.add_argument("--reset", action="store_true", help="(compat; sandboxes are always re-materialized)")

    verb("status", cmd_status, "workspace summary", dry=False, lock=False)

    sp = verb("evolve", cmd_evolve, "run the evolution loop (Algorithm 1)")
    sp.add_argument("--iters", type=int, default=3,
                    help="run iterations next_iter..ITERS (0 = baseline only)")
    sp.add_argument("--model")
    sp.add_argument("--provider")
    sp.add_argument("--max-turns", type=int, default=15)
    sp.add_argument("--max-parallel", type=int, default=1)
    sp.add_argument("--no-early-stop", action="store_true")

    sp = verb("gate", cmd_gate, "run a split with the active skills")
    sp.add_argument("--iter", type=int, default=0)
    sp.add_argument("--split", choices=["train", "val"], default="val")
    sp.add_argument("--model")
    sp.add_argument("--max-turns", type=int, default=15)
    sp.add_argument("--max-parallel", type=int, default=1)
    sp.add_argument("--overwrite", action="store_true", help="replace existing traces")

    sp = verb("run-task", cmd_run_task, "single inference rollout")
    sp.add_argument("task_id")
    sp.add_argument("--iter", type=int, default=1)
    sp.add_argument("--model")
    sp.add_argument("--max-turns", type=int, default=15)
    sp.add_argument("--run-budget", type=int, default=300)
    sp.add_argument("--overwrite", action="store_true", help="replace an existing trace")

    sp = verb("maintain", cmd_maintain, "run the wiki maintainer for an iteration")
    sp.add_argument("--iter", type=int, default=1)
    sp = verb("propose", cmd_propose, "run the skill proposer for an iteration")
    sp.add_argument("--iter", type=int, default=1)
    verb("reset", cmd_reset, "clear raw/ runs/ and roll skills back to the last commit")

    sp = verb("eval", cmd_eval, "held-out evaluation with active or empty skills")
    sp.add_argument("--tasks", required=True, help="tasks file (e.g. the bench's test.json)")
    sp.add_argument("--skills", choices=["active", "empty"], default="active")
    sp.add_argument("--repeat", type=int, default=1)
    sp.add_argument("--tag")
    sp.add_argument("--model")
    sp.add_argument("--max-turns", type=int, default=15)
    sp.add_argument("--run-budget", type=int, default=300)
    sp.add_argument("--max-parallel", type=int, default=1)
    sp.add_argument("--overwrite", action="store_true")

    sp = verb("compare", cmd_compare, "paired comparison of two workspaces (val)", ws=False)
    sp.add_argument("ws_a")
    sp.add_argument("ws_b")
    sp.add_argument("--iters", type=int, default=3)
    sp.add_argument("--max-turns", type=int, default=15)

    sp = verb("transfer", cmd_transfer, "copy active skills between workspaces", ws=False)
    sp.add_argument("src")
    sp.add_argument("dst")
    sp.add_argument("--force", action="store_true")

    sp = verb("report", cmd_report, "static HTML/JSON report of a workspace", dry=False, lock=False)
    sp.add_argument("--html", help="write the dashboard HTML here")
    sp.add_argument("--acceptance-dir", default=None)
    sp.add_argument("--doctor-json", default=None)
    sp.add_argument("--profile", default="wikiskill", help="profile the live doctor checks")

    for register in _extra_verbs():
        register(sub, verb)
    return p


def _extra_verbs():
    """TASK 3 verbs (doctor, setup-profile, promote, demote): ``ops.VERBS`` = register(sub, verb) callables."""
    try:
        from . import ops  # noqa: F401
    except ImportError:
        return []
    return ops.VERBS


def _emit(kind: str, data, as_json: bool) -> None:
    if kind is None:
        return
    if as_json:
        print(json.dumps(data, default=str))
        return
    try:
        from . import fmt
        print(fmt.render(kind, data))
    except ImportError:
        print(json.dumps(data, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.hermes:
        os.environ["HERMES_BIN"] = args.hermes
    if args.source_profile:
        os.environ["HWS_SOURCE_PROFILE"] = args.source_profile
    try:
        out = args.fn(args)
        kind, data = out[0], out[1]
        _emit(kind, data, args.json)
        return out[2] if len(out) > 2 else 0
    except HwsError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
