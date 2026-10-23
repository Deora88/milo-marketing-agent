import os

# Isolated in-memory MongoDB + safe defaults, set before any app import. MILO_ENV_FILE="" = ignore .env
# (also in the MCP server subprocess), so personal keys and caps never change test results.
os.environ["MILO_ENV_FILE"] = ""
os.environ["MONGODB_URI"] = "mongomock://"
os.environ["MONGODB_DB"] = "milo_test"
os.environ["META_DRY_RUN"] = "true"
os.environ["META_ALLOW_SPEND"] = "false"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["DM_AGENT_ENABLED"] = "false"  # tests call the DM agent directly with a fake model
os.environ.pop("API_TOKEN", None)

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
