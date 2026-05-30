"""
admin.py - Command-line user management for the MTG Rules Oracle.

Run on the same host the server runs on (so it touches the same users.db).

  python admin.py list
  python admin.py create  alice@example.com [--admin] [--approved]
  python admin.py approve alice@example.com
  python admin.py revoke  alice@example.com
  python admin.py make-admin alice@example.com
  python admin.py reset   alice@example.com [--base-url https://oracle.example.com]
  python admin.py delete  alice@example.com [--yes]

Bootstrap: the very first time you deploy, create an approved admin user with
  python admin.py create you@example.com --admin --approved
Then registrations from anyone else will land as un-approved until you run
  python admin.py approve their@email.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

import auth


def cmd_list(_):
    users = auth.list_users()
    if not users:
        print("(no users)")
        return
    print(f"{'EMAIL':36} {'APPR':5} {'ADMIN':6} {'OPUS':5} {'BUDGET/DAY':12} CREATED")
    for u in users:
        raw = u["daily_budget_micros"]
        if raw is None:
            budget = "default"
        elif raw < 0:
            budget = "unlimited"
        else:
            budget = f"${raw / 1_000_000:.2f}"
        opus = "yes" if (u["is_admin"] or u["opus_allowed"]) else "no"
        print(
            f"{u['email']:36} "
            f"{'yes' if u['approved'] else 'no':5} "
            f"{'yes' if u['is_admin'] else 'no':6} "
            f"{opus:5} "
            f"{budget:12} "
            f"{u['created_at']}"
        )


def cmd_create(args):
    pw = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw != pw2:
        sys.exit("Passwords do not match.")
    try:
        uid = auth.create_user(args.email, pw)
    except Exception as e:
        sys.exit(f"Failed: {e}")
    if args.approved:
        auth.set_approved(args.email, True)
    if args.admin:
        auth.set_admin(args.email, True)
    print(
        f"Created user id={uid} email={args.email} "
        f"approved={bool(args.approved)} admin={bool(args.admin)}"
    )


def cmd_approve(args):
    if auth.set_approved(args.email, True):
        print(f"Approved {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_revoke(args):
    if auth.set_approved(args.email, False):
        print(f"Revoked approval for {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_make_admin(args):
    if auth.set_admin(args.email, True):
        print(f"{args.email} is now admin")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_opus(args):
    allowed = args.on  # argparse guarantees exactly one of --on/--off
    if auth.set_opus_allowed(args.email, allowed):
        state = "enabled" if allowed else "disabled"
        print(f"Opus access {state} for {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def cmd_reset(args):
    user = auth.get_user_by_email(args.email.lower())
    if not user:
        sys.exit(f"No such user: {args.email}")
    token = auth.create_reset_token(user["id"])
    base = args.base_url or os.getenv("APP_BASE_URL", "http://localhost:8000")
    print("Single-use reset link (valid for 24 hours):")
    print(f"  {base.rstrip('/')}/reset?token={token}")


def cmd_delete(args):
    if not args.yes:
        ans = input(f"Delete {args.email}? [y/N] ")
        if ans.strip().lower() != "y":
            print("Aborted")
            return
    if auth.delete_user(args.email):
        print(f"Deleted {args.email}")
    else:
        sys.exit(f"No such user: {args.email}")


def _fmt_usd(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}"


def cmd_budget(args):
    if args.unlimited:
        micros = -1
    elif args.default:
        micros = None
    elif args.usd is not None:
        if args.usd < 0:
            sys.exit("Use --unlimited for no cap; --usd must be >= 0.")
        micros = int(round(args.usd * 1_000_000))
    else:
        sys.exit("Specify one of --usd N, --unlimited, or --default.")

    if not auth.set_daily_budget(args.email, micros):
        sys.exit(f"No such user: {args.email}")

    if micros is None:
        print(f"{args.email}: daily budget reset to default "
              f"({_fmt_usd(auth.DEFAULT_DAILY_BUDGET_MICROS)}/day).")
    elif micros < 0:
        print(f"{args.email}: daily budget set to UNLIMITED.")
    else:
        print(f"{args.email}: daily budget set to {_fmt_usd(micros)}/day.")


def cmd_usage(args):
    s = auth.usage_summary_today(args.email)
    if s is None:
        sys.exit(f"No such user: {args.email}")
    print(f"{s['email']} - usage today (resets 00:00 UTC):")
    print(f"  spent:     {_fmt_usd(s['spent_micros'])}")
    if s["unlimited"]:
        print("  budget:    unlimited")
    else:
        print(f"  budget:    {_fmt_usd(s['budget_micros'])}")
        print(f"  remaining: {_fmt_usd(s['remaining_micros'])}")


def main():
    auth.init_db()
    p = argparse.ArgumentParser(description="MTG Oracle user admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    sp = sub.add_parser("create")
    sp.add_argument("email")
    sp.add_argument("--admin", action="store_true")
    sp.add_argument("--approved", action="store_true")
    sp.set_defaults(func=cmd_create)

    for name, func in [
        ("approve", cmd_approve),
        ("revoke", cmd_revoke),
        ("make-admin", cmd_make_admin),
    ]:
        sp = sub.add_parser(name)
        sp.add_argument("email")
        sp.set_defaults(func=func)

    sp = sub.add_parser("reset")
    sp.add_argument("email")
    sp.add_argument("--base-url", help="Base URL for the reset link (default $APP_BASE_URL or http://localhost:8000)")
    sp.set_defaults(func=cmd_reset)

    sp = sub.add_parser("delete")
    sp.add_argument("email")
    sp.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser("budget", help="Set a user's daily spend cap")
    sp.add_argument("email")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--usd", type=float, help="Daily cap in US dollars, e.g. 2.50")
    g.add_argument("--unlimited", action="store_true", help="No daily cap")
    g.add_argument("--default", action="store_true", help="Use the global default")
    sp.set_defaults(func=cmd_budget)

    sp = sub.add_parser("usage", help="Show a user's spend so far today")
    sp.add_argument("email")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("opus", help="Grant or revoke Opus model access")
    sp.add_argument("email")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", action="store_true", help="Allow this user to use Opus")
    g.add_argument("--off", action="store_true", help="Disallow Opus for this user")
    sp.set_defaults(func=cmd_opus)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
