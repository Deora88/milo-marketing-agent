"""Check that Milo can reach MongoDB with the settings in .env (same connection code as the app).

    uv run python scripts/check_mongo.py

Prints the server's ping answer, or the error (Atlas: check MONGODB_URI and Network Access > your IP).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import store  # noqa: E402


def main() -> int:
    s = get_settings()
    host = s.mongodb_uri.split("://", 1)[-1].split("@")[-1].split("/")[0]  # never print the password
    try:
        print(f"MongoDB {host}, database {s.mongodb_db}:", store.db().client.admin.command("ping"))
        return 0
    except Exception as exc:  # noqa: BLE001 - a check script: show the reason
        print(f"MongoDB {host} not reachable: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
