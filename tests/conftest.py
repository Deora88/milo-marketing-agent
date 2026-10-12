import os
import tempfile

# Isolated test DB + safe defaults, set before any app import.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["META_DRY_RUN"] = "true"
os.environ["METRICS_PROVIDER"] = "simulated"

import pytest  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

from app.db.session import engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    SQLModel.metadata.drop_all(engine)
    init_db()
    yield
