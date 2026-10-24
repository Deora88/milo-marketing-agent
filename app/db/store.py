"""Small MongoDB data layer used by the API, the MCP server, the optimizer and the jobs.

One client per process (the web server and the MCP server are separate processes; both use the same database).
"""
from functools import lru_cache
from typing import Any, TypeVar

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.database import Database

from app.config import get_settings
from app.db.models import Approval, AuditLog, Campaign, Doc, MetricSnapshot

T = TypeVar("T", bound=Doc)


@lru_cache
def db() -> Database:
    s = get_settings()
    if s.mongodb_uri.startswith("mongomock://"):
        import mongomock  # dev dependency, tests only

        client: Any = mongomock.MongoClient(tz_aware=False)
    else:
        client = MongoClient(s.mongodb_uri, serverSelectionTimeoutMS=5000, appname="milo")
    return client[s.mongodb_db]


def init_db() -> None:
    d = db()
    d[Approval.collection].create_index([("status", ASCENDING), ("campaign_id", ASCENDING)])
    d[MetricSnapshot.collection].create_index([("campaign_id", ASCENDING), ("date", ASCENDING)])
    # No index on _id: MongoDB creates it automatically and rejects a custom {_id: -1} index
    # (mongomock does not). Newest-first sorts on _id use the built-in index.
    d[Campaign.collection].create_index([("status", ASCENDING)])


def next_id(collection: str) -> int:
    doc = db()["counters"].find_one_and_update(
        {"_id": collection}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return int(doc["seq"])


def _to_doc(obj: Doc) -> dict:
    data = obj.model_dump()
    data["_id"] = data.pop("id")
    return data


def _from_doc(cls: type[T], doc: dict | None) -> T | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc["id"] = doc.pop("_id")
    return cls.model_validate(doc)


def insert(obj: T) -> T:
    obj.id = next_id(obj.collection)
    db()[obj.collection].insert_one(_to_doc(obj))
    return obj


def save(obj: Doc) -> None:
    db()[obj.collection].replace_one({"_id": obj.id}, _to_doc(obj), upsert=True)


def get(cls: type[T], id_: int) -> T | None:
    return _from_doc(cls, db()[cls.collection].find_one({"_id": id_}))


def find(cls: type[T], filt: dict | None = None, sort: list | None = None, limit: int = 0) -> list[T]:
    cur = db()[cls.collection].find(filt or {})
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return [_from_doc(cls, d) for d in cur]


def count(cls: type[Doc], filt: dict | None = None) -> int:
    return db()[cls.collection].count_documents(filt or {})


def claim(cls: type[T], id_: int, expect: dict, changes: dict) -> T | None:
    """Atomically update a document only if it still matches `expect`. Returns the updated document, or None
    if someone else changed it first (e.g. a second Approve click)."""
    doc = db()[cls.collection].find_one_and_update(
        {"_id": id_, **expect}, {"$set": changes}, return_document=ReturnDocument.AFTER
    )
    return _from_doc(cls, doc)


def audit(event: str, **detail) -> None:
    insert(AuditLog(event=event, detail=detail))
