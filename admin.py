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
    print(f"{'EMAIL':40} {'APPROVED':10} {'ADMIN':6} CREATED")
    for u in users:
        print(
            f"{u['email']:40} "
            f"{'yes' if u['approved'] else 'no':10} "
            f"{'yes' if u['is_admin'] else 'no':6} "
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

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
