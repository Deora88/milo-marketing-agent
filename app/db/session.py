from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

engine = create_engine(get_settings().database_url, connect_args={"check_same_thread": False})


# Columns added after the first release. create_all() never alters an existing table, so an older
# milo.db gets them here (SQLite ADD COLUMN is cheap and keeps all rows).
ADDED_COLUMNS = {
    "approval": {"reason": "VARCHAR NOT NULL DEFAULT ''", "source": "VARCHAR NOT NULL DEFAULT 'agent'"},
    "campaign": {"location_name": "VARCHAR NOT NULL DEFAULT ''", "latitude": "FLOAT", "longitude": "FLOAT",
                 "radius_km": "FLOAT"},
}


def init_db() -> None:
    from app.db import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def get_session():
    with Session(engine) as session:
        yield session


def audit(session: Session, event: str, **detail) -> None:
    from app.db.models import AuditLog

    session.add(AuditLog(event=event, detail=detail))
