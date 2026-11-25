"""Manage Milo users from the command line (same rules as the Users API).

    uv run python scripts/add_user.py someone@example.com --role editor --name "Priya"   # new user (asks for a password)
    uv run python scripts/add_user.py someone@example.com --set-role admin              # change the role
    uv run python scripts/add_user.py someone@example.com --reset-password              # new password
    uv run python scripts/add_user.py someone@example.com --disable                     # block sign-in (--enable undoes it)
    uv run python scripts/add_user.py --list                                            # everyone and their role

A role change, new password or disabled account signs that person out at once. Milo always keeps at least one
active admin. Passwords are asked for and never echoed.
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth  # noqa: E402
from app.db import store  # noqa: E402
from app.db.models import User  # noqa: E402


def _ask_password(email: str) -> str | None:
    password = getpass.getpass(f"Password for {email} (at least {auth.MIN_PASSWORD} characters): ")
    if getpass.getpass("Again: ") != password:
        print("The two passwords are different. Nothing was changed.")
        return None
    return password


def _find(email: str) -> User | None:
    rows = store.find(User, {"email": email.strip().lower()}, limit=1)
    if not rows:
        print(f"No user with the email {email}. Add them first (without --set-role).")
        return None
    return rows[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Add Milo users, change their role, reset a password, or list users.")
    ap.add_argument("email", nargs="?")
    ap.add_argument("--role", choices=auth.ROLES, default="editor", help="role for a NEW user (default: editor)")
    ap.add_argument("--name", default="")
    ap.add_argument("--set-role", choices=auth.ROLES, help="change the role of an existing user")
    ap.add_argument("--reset-password", action="store_true")
    ap.add_argument("--disable", action="store_true", help="block sign-in for this user")
    ap.add_argument("--enable", action="store_true", help="allow sign-in again")
    ap.add_argument("--list", action="store_true", help="show all users")
    args = ap.parse_args()
    store.init_db()

    if args.list:
        for u in store.find(User, sort=[("_id", 1)]):
            print(f"{u.email:40} {u.role:7} {'DISABLED' if u.disabled else 'active'}  {u.name}")
        return 0
    if not args.email:
        ap.error("give an email (or --list)")
    try:
        if args.set_role or args.reset_password or args.disable or args.enable:
            u = _find(args.email)
            if u is None:
                return 1
            changes: dict = {}
            if args.set_role:
                changes["role"] = args.set_role
            if args.disable or args.enable:
                changes["disabled"] = bool(args.disable)
            if args.reset_password:
                pw = _ask_password(u.email)
                if pw is None:
                    return 1
                changes["password"] = pw
            u = auth.update_user(u.id, **changes)
            print(f"Updated {u.email}: role {u.role}, {'disabled' if u.disabled else 'active'}. "
                  "They are signed out and sign in again with the new settings.")
        else:
            pw = _ask_password(args.email)
            if pw is None:
                return 1
            u = auth.create_user(args.email, pw, args.role, args.name)
            print(f"Added {u.email} as {u.role}.")
    except auth.AuthError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
