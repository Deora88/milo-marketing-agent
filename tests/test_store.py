from app.db.store import db, init_db


def test_init_db_only_creates_indexes_real_mongodb_accepts():
    """Real MongoDB rejects any custom index on _id (e.g. {_id: -1}); mongomock does not, so check it here."""
    init_db()
    for name in db().list_collection_names():
        for idx_name, info in db()[name].index_information().items():
            keys = [k for k, _ in info["key"]]
            if "_id" in keys:
                assert idx_name == "_id_" and info["key"] == [("_id", 1)], (name, idx_name, info["key"])
