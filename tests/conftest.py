import os
import re
from pathlib import Path

# Isolated in-memory MongoDB + safe defaults, set before any app import. MILO_ENV_FILE="" = ignore .env
# (also in the MCP server subprocess), so personal keys and caps never change test results.
# Milo settings already in the environment are removed too: `uv run --env-file .env`, UV_ENV_FILE or a
# terminal where .env was loaded would otherwise put real keys, caps and Meta ids into the tests.
_FIELDS = re.findall(r"^    ([a-z][a-z0-9_]*)\s*:", (Path(__file__).parents[1] / "app" / "config.py").read_text(),
                     re.M)
for _name in {f.upper() for f in _FIELDS} | {"API_TOKEN"}:
    os.environ.pop(_name, None)
os.environ["MILO_ENV_FILE"] = ""
# litellm loads the nearest .env into the environment when it is imported (its "DEV" mode); with .venv inside
# the project that is your real .env. PRODUCTION mode turns that off.
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["MONGODB_URI"] = "mongomock://"
os.environ["MONGODB_DB"] = "milo_test"
os.environ["META_DRY_RUN"] = "true"
os.environ["META_ALLOW_SPEND"] = "false"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["DM_AGENT_ENABLED"] = "false"  # tests call the DM agent directly with a fake model

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.store import db, init_db  # noqa: E402

WEB = {"X-Milo-Client": "web"}  # header the UI sends on every change request


@pytest.fixture(autouse=True)
def fresh_db():
    get_settings.cache_clear()
    d = db()
    for name in d.list_collection_names():
        d.drop_collection(name)
    init_db()
    yield
    get_settings.cache_clear()
