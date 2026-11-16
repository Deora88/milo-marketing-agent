"""Add a Milo user from the command line (same rules as the Users API; the password is asked for, not echoed).

    uv run python scripts/add_user.py someone@example.com --role editor --name "Priya"
    uv run python scripts/add_user.py someone@example.com --reset-password
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth  # noqa: E402
from app.db import store  # noqa: E402
from app.db.models import User  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Add a Milo user or reset a password.")
    ap.add_argument("email")
    ap.add_argument("--role", choices=auth.ROLES, default="editor")
    ap.add_argument("--name", default="")
    ap.add_argument("--reset-password", action="store_true")
    args = ap.parse_args()
    store.init_db()
    password = getpass.getpass(f"Password for {args.email} (at least {auth.MIN_PASSWORD} characters): ")
    if getpass.getpass("Again: ") != password:
        print("The two passwords are different. Nothing was changed.")
        return 1
    try:
        if args.reset_password:
            rows = store.find(User, {"email": args.email.strip().lower()}, limit=1)
            if not rows:
                print("No user with that email.")
                return 1
            auth.update_user(rows[0].id, password=password)
            print(f"Password changed for {rows[0].email}. They are signed out everywhere.")
        else:
            u = auth.create_user(args.email, password, args.role, args.name)
            print(f"Added {u.email} as {u.role}.")
    except auth.AuthError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
