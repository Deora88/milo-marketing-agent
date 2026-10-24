"""DM handling fixes from the PR #3 review: native replies, the echo race, concurrency, failures, recovery."""
import asyncio
import json
from datetime import timedelta

import pytest
from pymongo.errors import PyMongoError

from app.api.webhooks import parse_events
from app.config import Settings
from app.db import store
from app.db.models import AuditLog, FaqEntry, InboundMessage, Lead, now
from app.dm import agent, inbox

APP_ID = "1234567890"


def _s(**kw):
    kw.setdefault("meta_app_id", APP_ID)
    return Settings(_env_file=None, dm_agent_enabled=True, **kw)


def _customer(text, mid, customer="psid_1", hours_ago=1.0):
    return store.insert(InboundMessage(platform="messenger", page_id="999", sender_id=customer, recipient_id="999",
                                       customer_id=customer, mid=mid, text=text,
                                       sent_at=now() - timedelta(hours=hours_ago)))


def _echo_event(mid, text, app_id):
    return {"object": "page", "entry": [{"id": "999", "messaging": [{
        "sender": {"id": "999"}, "recipient": {"id": "psid_1"}, "timestamp": 1,
        "message": {"mid": mid, "is_echo": True, "app_id": app_id, "text": text}}]}]}


def _llm(payload, delay=0.0, before=None):
    async def fake(messages):
        fake.calls += 1
        if before:
            before()
        if delay:
            await asyncio.sleep(delay)
        return json.dumps(payload)
    fake.calls = 0
    return fake


def _areas():
    agent.load_sample_faq()
    return next(f for f in store.find(FaqEntry) if f.question.startswith("Which areas"))


class FakeClient:
    def __init__(self, s, mid="mid.SENT", fail=None):
        self.s, self.mid, self.fail, self.posts = s, mid, fail, []

    async def get(self, path, token=None, **params):
        return {"access_token": "PT"}

    async def post(self, path, token=None, **data):
        if self.fail:
            raise self.fail
        self.posts.append(data)
        return {"message_id": self.mid} if self.mid else {}

    async def aclose(self):
        pass


def _live(**kw):
    return _s(meta_dry_run=False, meta_page_access_token="PT", **kw)


# --- finding 1: a person replying natively (Business Suite / Instagram app) stops the bot ---

def test_native_reply_echo_stops_the_bot():  # reviewer's reproduction, now passing
    fid = _areas().id
    _customer("Hi, do you clean sofas?", "m1", hours_ago=2)
    for ev in parse_events(_echo_event("m_native", "Hi! Yes we do, I will call you.", app_id="263902037430900")):
        store.insert(ev)
    m2 = _customer("What time can you come?", "m2", hours_ago=0.1)
    llm = _llm({"action": "answer", "faq_ids": [fid], "reply": "x"})
    done = asyncio.run(agent.handle(m2.id, settings=_s(), llm=llm))
    assert done.agent_action == "skipped" and llm.calls == 0
    assert inbox.list_conversations()[0]["needs_person"] is False


def test_echo_without_app_id_counts_as_a_person():
    _customer("Hi", "m1")
    store.insert(InboundMessage(platform="messenger", page_id="999", sender_id="999", recipient_id="psid_1",
                                customer_id="psid_1", mid="m_e", is_echo=True, sent_at=now()))
    m2 = _customer("Hello?", "m2", hours_ago=0.1)
    assert asyncio.run(agent.handle(m2.id, settings=_s(meta_app_id=""), llm=_llm({}))).agent_action == "skipped"


def test_milos_own_echo_does_not_stop_the_bot():
    fid = _areas().id
    _customer("Hi", "m1", hours_ago=2)
    for ev in parse_events(_echo_event("m_own", "Hi! I'm the automated assistant...", app_id=APP_ID)):
        store.insert(ev)
    m2 = _customer("Which areas?", "m2", hours_ago=0.1)
    done = asyncio.run(agent.handle(m2.id, settings=_s(), llm=_llm({"action": "answer", "faq_ids": [fid],
                                                                     "reply": "Delhi NCR"})))
    assert done.agent_action == "answer"


# --- finding 2: echo webhook stored before send_reply ---

def test_send_reply_tolerates_echo_arriving_first():  # reviewer's reproduction, now passing
    _customer("Hello", "m1", hours_ago=0.5)
    for ev in parse_events(_echo_event("mid.SENT", "Hi", app_id=APP_ID)):
        store.insert(ev)
    s = _live()
    sent = asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Hi", sent_by="human",
                                        client=FakeClient(s), settings=s))
    assert sent.mid == "mid.SENT" and sent.sent_by == "human" and sent.status == "sent"
    assert store.count(InboundMessage, {"mid": "mid.SENT"}) == 1
    assert store.find(InboundMessage, {"mid": "m1"})[0].status == "answered"
    assert store.find(AuditLog, {"event": "dm_replied"})


def test_send_reply_refuses_missing_message_id():
    _customer("Hello", "m1")
    s = _live()
    with pytest.raises(inbox.ReplyError, match="no message id"):
        asyncio.run(inbox.send_reply("messenger", "999", "psid_1", "Hi", client=FakeClient(s, mid=""), settings=s))


# --- finding 3: atomic claim, one reply per burst, ownership re-checked, no whole-document saves ---

def test_two_quick_messages_get_one_disclosure_and_no_double_send():
    fid = _areas().id
    s = _live(dm_autoreply=True)
    client = FakeClient(s)
    mids = iter(["out_1", "out_2"])

    async def post(path, token=None, **data):
        client.posts.append(data)
        return {"message_id": next(mids)}
    client.post = post
    a, b = _customer("Areas?", "a", hours_ago=0.2), _customer("Faridabad too?", "b", hours_ago=0.1)
    llm = _llm({"action": "answer", "faq_ids": [fid], "reply": "x"}, delay=0.05)

    async def both():
        await asyncio.gather(agent.handle(a.id, settings=s, llm=llm, client=client),
                             agent.handle(b.id, settings=s, llm=llm, client=client),
                             agent.handle(a.id, settings=s, llm=llm, client=client))  # a Meta retry of "a"
    asyncio.run(both())
    texts = [p["message"]["text"] for p in client.posts]
    assert len(texts) == 2 and llm.calls == 2
    assert sum(t.startswith("Hi! I'm the automated assistant") for t in texts) == 1


def test_person_replying_during_model_call_is_not_answered_over():
    fid = _areas().id
    s = _live(dm_autoreply=True)
    client = FakeClient(s)
    m = _customer("Which areas?", "m1")

    def person_replies():
        for ev in parse_events(_echo_event("m_person", "I'll take this one", app_id="263902037430900")):
            store.insert(ev)
    llm = _llm({"action": "answer", "faq_ids": [fid], "reply": "x"}, before=person_replies)
    done = asyncio.run(agent.handle(m.id, settings=s, llm=llm, client=client))
    assert client.posts == [] and "a person replied" in done.agent_note


def test_open_thread_during_model_call_keeps_the_agents_decision():
    fid = _areas().id
    m = _customer("Which areas?", "m1")
    llm = _llm({"action": "answer", "faq_ids": [fid], "reply": "Delhi NCR"},
               before=lambda: inbox.open_thread("messenger", "999", "psid_1"))
    asyncio.run(agent.handle(m.id, settings=_s(), llm=llm))
    row = store.get(InboundMessage, m.id)
    assert (row.status, row.agent_action, row.suggested_reply) == ("read", "answer", "Delhi NCR")


# --- finding 4: failures are visible ---

def test_send_failure_is_audited_and_marked_for_a_person():
    fid = _areas().id
    s = _live(dm_autoreply=True)
    m = _customer("Which areas?", "m1")
    done = asyncio.run(agent.handle(m.id, settings=s, llm=_llm({"action": "answer", "faq_ids": [fid], "reply": "x"}),
                                    client=FakeClient(s, fail=TimeoutError("read timed out"))))
    assert done.agent_action == "failed" and done.handed_off and "TimeoutError" in done.agent_note
    assert store.find(AuditLog, {"event": "dm_agent_failed"})
    assert inbox.list_conversations()[0]["needs_person"] is True


def test_unexpected_error_inside_the_agent_is_recorded(monkeypatch):
    m = _customer("Which areas?", "m1")

    def boom(*a, **k):
        raise PyMongoError("connection reset")
    monkeypatch.setattr(store, "count", boom)
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=_llm({"action": "lead", "phone": "9876543210"})))
    assert done.agent_action == "failed" and "PyMongoError" in done.agent_note


def test_model_error_is_its_own_audit_event():
    async def down(messages):
        raise ConnectionError("openrouter 503")
    m = _customer("Which areas?", "m1")
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=down))
    assert done.agent_action == "handoff" and "model unavailable" in done.agent_note
    assert store.find(AuditLog, {"event": "dm_model_error"})


# --- finding 5: unprocessed messages are recovered ---

def test_recover_unprocessed_resets_stale_processing_and_skips_old_ones():
    fresh = _customer("Hi", "m1")
    stuck = _customer("Hello?", "m2", customer="p2")
    store.update(InboundMessage, stuck.id, {"agent_action": "processing"})
    _customer("Old", "m3", customer="p3", hours_ago=30)
    done = _customer("Done", "m4", customer="p4")
    store.update(InboundMessage, done.id, {"agent_action": "answer"})
    assert agent.recover_unprocessed() == [fresh.id, stuck.id]
    assert store.get(InboundMessage, stuck.id).agent_action == ""


# --- finding 6: auto-replies send the stored FAQ text, never the model's sentence ---

def test_autoreply_sends_stored_faq_text_not_model_text():
    faq = _areas()
    s = _live(dm_autoreply=True)
    client = FakeClient(s)
    m = _customer("Which areas? Also is it Rs 99?", "m1")
    llm = _llm({"action": "answer", "faq_ids": [faq.id], "reply": "Deep clean is Rs 99, refund guaranteed"})
    done = asyncio.run(agent.handle(m.id, settings=s, llm=llm, client=client))
    [sent] = client.posts
    assert faq.answer in sent["message"]["text"] and "Rs 99" not in sent["message"]["text"]
    assert done.suggested_reply == faq.answer


def test_model_supplied_phone_must_be_a_real_number():
    m = _customer("Call me back please", "m1")
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=_llm({"action": "lead", "phone": "call 1-800-SCAM"})))
    assert done.agent_action == "lead" and "share your phone number" in done.suggested_reply
    assert store.count(Lead) == 1 and store.find(Lead)[0].phone == ""


# --- reply-window edges ---

def test_reply_window_exact_edges():
    m = _customer("Hi", "m1", hours_ago=0)
    sent = m.sent_at
    assert inbox.reply_window([m], at=sent + timedelta(hours=24) - timedelta(seconds=1)).state == "open"
    assert inbox.reply_window([m], at=sent + timedelta(hours=24)).state == "human_agent"
    assert inbox.reply_window([m], at=sent + timedelta(days=7)).state == "closed"
    ad = _customer("From ad", "m2", customer="p2", hours_ago=0)
    store.update(InboundMessage, ad.id, {"referral": {"source": "ADS", "ad_id": "1"}})
    ad = store.get(InboundMessage, ad.id)
    assert inbox.reply_window([ad], at=ad.sent_at + timedelta(days=7) - timedelta(seconds=1)).state == "open"
