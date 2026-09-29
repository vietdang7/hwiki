"""CLI verbs added in TASK 3: setup-profile, doctor, promote, demote (registered by cli)."""

from __future__ import annotations


from . import doctor, profiles, promote as promote_mod


def _cmd_setup_profile(args):
    return "setup-profile", profiles.setup_profile(args.profile, source_profile=args.source_profile,
                                                   i_am_owner=args.i_am_owner, dry_run=args.dry_run,
                                                   remove=args.remove)


def _cmd_doctor(args):
    out = doctor.run_checks(args.profile, live=args.live, hermes=args.hermes, ws_root=args.ws_root)
    return "doctor", out, 0 if out["ok"] else 1


def _cmd_promote(args):
    from .cli import _locked, resolve_ws
    ws = resolve_ws(args)
    skills = [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills else None
    with _locked(ws, args, dry_run=args.dry_run):
        out = promote_mod.promote(ws, args.profile, skills=skills, replace=args.replace,
                                  i_am_owner=args.i_am_owner, dry_run=args.dry_run)
    return "promote", out


def _cmd_demote(args):
    return "demote", promote_mod.demote(args.profile, receipt_id=args.receipt, last=args.last,
                                        i_am_owner=args.i_am_owner, dry_run=args.dry_run)


def _register(sub, verb):
    sp = verb("setup-profile", _cmd_setup_profile,
              "create/refresh the dedicated Hermes profile (minimal creds, operator skill)",
              ws=False, lock=False)
    sp.add_argument("--profile", default=profiles.DEFAULT_PROFILE)
    sp.add_argument("--i-am-owner", action="store_true",
                    help="owner approval for an existing profile hermes-wikiskill did not create (incl. "
                         "`default`): installs ONLY the operator skill (backup + receipt); never touches "
                         "its config.yaml/.env/auth.json")
    sp.add_argument("--remove", action="store_true",
                    help="undo the operator skill installed into a foreign profile (restore backup or "
                         "delete; needs --i-am-owner; exit 4 if it changed since)")

    sp = verb("doctor", _cmd_doctor, "health checks (exit 0 iff no fail)", ws=False, dry=False, lock=False)
    sp.add_argument("--profile", default=profiles.DEFAULT_PROFILE)
    sp.add_argument("--live", action="store_true", help="one real rollout proving skills visibility")

    sp = verb("promote", _cmd_promote, "copy gate-ACCEPTED skills into a Hermes profile (backup + receipt)")
    sp.add_argument("--profile", required=True)
    sp.add_argument("--skills", help="comma-separated subset of the accepted skills")
    sp.add_argument("--replace", action="store_true", help="allow replacing same-named skills")
    sp.add_argument("--i-am-owner", action="store_true",
                    help="needed for any profile hermes-wikiskill did not create (incl. `default`), "
                         "except hws-test-*")

    sp = verb("demote", _cmd_demote, "undo a promote from its backup", ws=False, lock=False)
    sp.add_argument("--profile", required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--receipt")
    g.add_argument("--last", action="store_true")
    sp.add_argument("--i-am-owner", action="store_true",
                    help="needed for any profile hermes-wikiskill did not create (incl. `default`), "
                         "except hws-test-*; undo of an owner promote: "
                         "hws demote --profile default --last --i-am-owner")


VERBS = [_register]
__all__ = ["VERBS"]
