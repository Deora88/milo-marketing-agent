"""Business PDFs for the DM bot (upload, reading, passages, drafts only) and the sender's name and id."""
import asyncio
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import store
from app.db.models import AuditLog, CustomerProfile, InboundMessage, KnowledgeDoc, now
from app.dm import agent, knowledge, profiles
from app.main import app
from app.meta.client import MetaAPIError
from tests.conftest import WEB
from tests.pdfmaker import make_pdf

PRICES = make_pdf(["Price list\nDeep clean for a 2 BHK costs Rs 3,999.\nSofa shampoo costs Rs 499 per seat.",
                   "Refund policy\nCancel up to 24 hours before the slot for a full refund."])


def _s(**kw):
    return Settings(_env_file=None, dm_agent_enabled=True, **kw)


def _customer(text, mid="m1", customer="psid_1", platform="messenger"):
    return store.insert(InboundMessage(platform=platform, page_id="999", sender_id=customer, recipient_id="999",
                                       customer_id=customer, mid=mid, text=text, sent_at=now() - timedelta(hours=1)))


def _llm(payload):
    async def fake(messages):
        fake.seen = messages
        return json.dumps(payload)
    fake.seen = None
    return fake


# --- PDF upload and reading ---

def test_pdf_text_is_extracted_page_by_page():
    doc = knowledge.add_document("prices.pdf", PRICES)
    assert len(doc.pages) == 2 and "Rs 3,999" in doc.pages[0] and "full refund" in doc.pages[1]
    assert doc.chars == sum(len(p) for p in doc.pages) and doc.sha256
    assert store.find(AuditLog, {"event": "knowledge_doc_added"})


def test_upload_checks():
    knowledge.add_document("prices.pdf", PRICES)
    for data, words in [(PRICES, "already uploaded"), (b"hello, not a pdf", "not a PDF"),
                        (make_pdf(["", ""]), "No text found"), (b"%PDF-1.4 garbage", "couldn't read")]:
        try:
            knowledge.add_document("x.pdf", data)
            raise AssertionError("should have been refused")
        except knowledge.DocError as exc:
            assert words in str(exc)


def test_page_and_size_limits(monkeypatch):
    monkeypatch.setattr(knowledge, "MAX_PAGES", 1)
    try:
        knowledge.extract_pages(PRICES)
        raise AssertionError
    except knowledge.DocError as exc:
        assert "limit is 1" in str(exc)
    monkeypatch.setattr(knowledge, "MAX_BYTES", 100)
    try:
        knowledge.extract_pages(PRICES)
        raise AssertionError
    except knowledge.DocError as exc:
        assert "larger than" in str(exc)


def test_filename_is_cleaned():
    assert knowledge.add_document("C:\\Users\\me\\<price> list.pdf", PRICES).filename == "_price_ list.pdf"


def test_knowledge_api_upload_read_toggle_delete():
    c = TestClient(app, headers=WEB)
    r = c.post("/api/knowledge", files={"file": ("prices.pdf", PRICES, "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["page_count"] == 2 and "pages" not in d and d["preview"].startswith("Price list")
    assert [x["id"] for x in c.get("/api/knowledge").json()] == [d["id"]]
    assert c.get(f"/api/knowledge/{d['id']}").json()["pages"][1].startswith("Refund policy")
    assert c.patch(f"/api/knowledge/{d['id']}", json={"enabled": False}).json()["enabled"] is False
    again = c.post("/api/knowledge", files={"file": ("copy.pdf", PRICES, "application/pdf")})
    assert again.status_code == 400 and "already uploaded" in again.json()["detail"]
    assert c.post("/api/knowledge", files={"file": ("a.pdf", b"nope", "application/pdf")}).status_code == 400
    assert c.delete(f"/api/knowledge/{d['id']}").json() == {"deleted": d["id"]}
    assert c.get(f"/api/knowledge/{d['id']}").status_code == 404
    assert TestClient(app).post("/api/knowledge", files={"file": ("p.pdf", PRICES)}).status_code == 403  # CSRF


def test_upload_size_limit_on_the_route(monkeypatch):
    monkeypatch.setattr(knowledge, "MAX_BYTES", 100)
    r = TestClient(app, headers=WEB).post("/api/knowledge", files={"file": ("p.pdf", PRICES, "application/pdf")})
    assert r.status_code == 413


def test_search_finds_matching_passages_from_enabled_docs_only():
    doc = knowledge.add_document("prices.pdf", PRICES)
    hits = knowledge.search("How much is sofa shampoo?")
    assert hits and hits[0]["ref"].startswith(f"D{doc.id}-") and "Sofa shampoo" in hits[0]["text"]
    assert hits[0]["page"] == 1
    assert knowledge.search("hello there") == []  # only stop words / no overlap
    store.update(KnowledgeDoc, doc.id, {"enabled": False})
    assert knowledge.search("How much is sofa shampoo?") == []


def test_long_pages_are_split_into_short_passages():
    text = " ".join(f"Sentence number {i} about cleaning." for i in range(200))
    doc = knowledge.add_document("long.pdf", make_pdf([text]))
    ps = knowledge.passages(doc)
    assert len(ps) > 5 and all(len(p["text"]) <= knowledge.PASSAGE_CHARS for p in ps)


# --- the DM bot uses PDF passages, drafts only ---

def test_bot_answers_from_a_pdf_passage_as_a_draft():
    knowledge.add_document("prices.pdf", PRICES)
    ref = knowledge.search("sofa shampoo price")[0]["ref"]
    m = _customer("What is the price for sofa shampoo?")
    llm = _llm({"action": "answer", "faq_ids": [], "doc_refs": [ref], "reply": "Sofa shampoo is Rs 499 per seat."})
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=llm))
    system = llm.seen[0]["content"]
    assert f"[{ref}] (prices.pdf, page 1)" in system and "doc_refs" in system
    assert done.agent_action == "answer" and done.suggested_reply == "Sofa shampoo is Rs 499 per seat."
    assert "PDF prices.pdf p.1" in done.agent_note


def test_invented_or_unshown_passage_ids_become_a_handoff():
    knowledge.add_document("prices.pdf", PRICES)
    m = _customer("What is the price for sofa shampoo?")
    for refs in (["D99-1"], ["D1-2"]):  # not existing / existing but not shown for this question
        store.update(InboundMessage, m.id, {"agent_action": ""})
        done = asyncio.run(agent.handle(m.id, settings=_s(), llm=_llm(
            {"action": "answer", "doc_refs": refs, "reply": "It's free today!"})))
        assert done.agent_action == "handoff", refs


def test_pdf_answer_is_never_auto_sent():
    knowledge.add_document("prices.pdf", PRICES)
    ref = knowledge.search("sofa shampoo price")[0]["ref"]
    m = _customer("What is the price for sofa shampoo?")
    sent = []

    class Client:
        async def post(self, *a, **k):
            sent.append(k)
            return {"message_id": "x"}

        async def get(self, *a, **k):
            return {"access_token": "PT"}

        async def aclose(self):
            pass
    s = _s(dm_autoreply=True, meta_dry_run=False, meta_page_access_token="PT")
    done = asyncio.run(agent.handle(m.id, settings=s, client=Client(), llm=_llm(
        {"action": "answer", "doc_refs": [ref], "reply": "Rs 499 per seat."})))
    assert done.agent_action == "answer" and "draft only" in done.agent_note and sent == []


def test_prompt_without_documents_is_unchanged():
    m = _customer("Which areas?")
    llm = _llm({"action": "handoff"})
    asyncio.run(agent.handle(m.id, settings=_s(), llm=llm))
    system = llm.seen[0]["content"]
    assert "You may ONLY use the FAQ below." in system and "doc_refs" not in system


# --- sender name and id ---

class ProfileClient:
    def __init__(self, data=None, fail=None):
        self.data, self.fail, self.calls = data or {}, fail, []
        self.s = _live()  # like MetaClient: the Page token comes from settings

    async def get(self, path, token=None, **params):
        self.calls.append((path, token, params))
        if self.fail:
            raise self.fail
        return self.data

    async def aclose(self):
        pass


def _live():
    return _s(meta_dry_run=False, meta_page_access_token="PT")


def test_messenger_name_is_looked_up_once_and_cached():
    c = ProfileClient({"first_name": "Rahul", "last_name": "Sharma", "id": "25104897412345"})
    p = asyncio.run(profiles.ensure("messenger", "999", "25104897412345", client=c, settings=_live()))
    assert p.name == "Rahul Sharma" and not p.error
    assert c.calls == [("/25104897412345", "PT", {"fields": "first_name,last_name"})]
    asyncio.run(profiles.ensure("messenger", "999", "25104897412345", client=c, settings=_live()))
    assert len(c.calls) == 1  # cached


def test_instagram_name_and_username():  # IG account 999 is linked to Facebook Page 555
    c = ProfileClient({"name": "Priya", "username": "priya.cleans"})
    c.s = _s(meta_dry_run=False, meta_page_access_token="PT", meta_page_id="555", meta_instagram_account_id="999")
    p = asyncio.run(profiles.ensure("instagram", "999", "igsid_1", client=c, settings=c.s))
    assert (p.name, p.username) == ("Priya", "priya.cleans") and c.calls[0][2] == {"fields": "name,username"}


def test_failed_lookup_is_recorded_and_retried_after_a_day():
    c = ProfileClient(fail=MetaAPIError(100, 33, "Unsupported get request"))
    p = asyncio.run(profiles.ensure("messenger", "999", "psid_9", client=c, settings=_live()))
    assert p.name == "" and "MetaAPIError" in p.error
    asyncio.run(profiles.ensure("messenger", "999", "psid_9", client=c, settings=_live()))
    assert len(c.calls) == 1  # not again within a day
    store.update(CustomerProfile, p.id, {"fetched_at": now() - timedelta(hours=25)})
    ok = ProfileClient({"first_name": "Asha"})
    assert asyncio.run(profiles.ensure("messenger", "999", "psid_9", client=ok, settings=_live())).name == "Asha"


def test_dry_run_never_calls_meta():
    c = ProfileClient({"first_name": "X"})
    assert asyncio.run(profiles.ensure("messenger", "999", "psid_1", client=c, settings=_s())) is None
    assert c.calls == []


def test_inbox_shows_sender_name_and_full_id():
    _customer("Hi", customer="25104897412345")
    store.insert(CustomerProfile(platform="messenger", customer_id="25104897412345", name="Rahul Sharma",
                                 fetched_at=now()))
    _customer("Hello", mid="m2", customer="psid_unknown")
    c = TestClient(app, headers=WEB)
    rows = {r["customer_id"]: r["sender"] for r in c.get("/api/inbox").json()}
    assert rows["25104897412345"] == {"id": "25104897412345", "name": "Rahul Sharma", "username": "",
                                      "lookup_error": ""}
    assert rows["psid_unknown"]["name"] == "" and rows["psid_unknown"]["id"] == "psid_unknown"
    t = c.get("/api/inbox/messenger/999/25104897412345").json()
    assert t["sender"]["name"] == "Rahul Sharma" and t["sender"]["id"] == "25104897412345"


def test_webhook_looks_up_new_senders(monkeypatch):
    import hashlib
    import hmac

    from app.config import get_settings
    seen = []

    async def record(keys):
        seen.extend(keys)
    monkeypatch.setattr(profiles, "ensure_many", record)
    monkeypatch.setenv("META_APP_SECRET", "sekret")
    get_settings.cache_clear()
    body = json.dumps({"object": "page", "entry": [{"id": "999", "messaging": [
        {"sender": {"id": "psid_7"}, "recipient": {"id": "999"}, "timestamp": 1, "message": {"mid": "w1", "text": "hi"}},
        {"sender": {"id": "999"}, "recipient": {"id": "psid_7"}, "timestamp": 2,
         "message": {"mid": "w2", "text": "hello", "is_echo": True}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"sekret", body, hashlib.sha256).hexdigest()
    r = TestClient(app).post("/webhooks/meta", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200 and seen == [("messenger", "999", "psid_7")]  # the customer, not the echo
