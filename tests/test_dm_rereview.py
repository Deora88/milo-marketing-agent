"""DM fixes from the PR #3 re-review: hand-off rule, whole FAQ answers, recovery task, idle locks."""
import asyncio

from fastapi.testclient import TestClient

from app.api import webhooks
from app.db import store
from app.db.models import AuditLog, FaqEntry, InboundMessage
from app.dm import agent, inbox
from tests.conftest import WEB
from tests.test_dm_hardening import (
    FakeClient,
    _areas,
    _customer,
    _echo_event,
    _live,
    _llm,
    _s,
)


def _handed_off(text, mid, hours_ago):
    m = _customer(text, mid, hours_ago=hours_ago)
    return store.update(InboundMessage, m.id, {"agent_action": "handoff", "handed_off": True})


def _answer(fid):
    return _llm({"action": "answer", "faq_ids": [fid], "reply": "We cover Delhi NCR."})


# --- MEDIUM: a hand-off is no longer permanent ---

def test_bot_handoff_expires_and_a_later_faq_question_is_answered():  # reviewer's probe, now passing
    fid = _areas().id
    _handed_off("Can I get a discount?", "m1", hours_ago=30)
    m2 = _customer("Which areas do you cover?", "m2", hours_ago=0.1)
    done = asyncio.run(agent.handle(m2.id, settings=_s(), llm=_answer(fid)))
    assert done.agent_action == "answer"


def test_bot_handoff_still_keeps_the_bot_out_inside_the_hold():
    _areas()
    _handed_off("Can I get a discount?", "m1", hours_ago=2)
    m2 = _customer("Which areas do you cover?", "m2", hours_ago=0.1)
    llm = _llm({})
    done = asyncio.run(agent.handle(m2.id, settings=_s(dm_handoff_hours=24), llm=llm))
    assert done.agent_action == "skipped" and "handed to a person" in done.agent_note and llm.calls == 0


def test_failed_send_does_not_silence_later_messages():  # reviewer's probe, now passing
    fid = _areas().id
    s = _live(dm_autoreply=True)
    m1 = _customer("Which areas?", "m1", hours_ago=0.5)
    failed = asyncio.run(agent.handle(m1.id, settings=s, llm=_answer(fid),
                                      client=FakeClient(s, fail=TimeoutError("read timed out"))))
    assert failed.agent_action == "failed" and not failed.handed_off
    assert inbox.list_conversations()[0]["needs_person"] is True  # still flagged for a person
    m2 = _customer("And which areas again?", "m2", hours_ago=0.1)
    client = FakeClient(s, mid="mid.OK")
    done = asyncio.run(agent.handle(m2.id, settings=s, llm=_answer(fid), client=client))
    assert done.agent_action == "answer" and len(client.posts) == 1


def test_hand_back_lets_the_bot_answer_after_a_person_replied():
    fid = _areas().id
    _customer("Hi", "m1", hours_ago=2)
    for ev in webhooks.parse_events(_echo_event("m_native", "Hi, I'll call you", app_id="263902037430900")):
        store.insert(ev)
    m2 = _customer("Which areas?", "m2", hours_ago=1)
    skipped = asyncio.run(agent.handle(m2.id, settings=_s(), llm=_answer(fid)))
    assert skipped.agent_action == "skipped" and "263902037430900" in skipped.agent_note  # names the app

    from app.main import app
    c = TestClient(app, headers=WEB)
    t = c.get("/api/inbox/messenger/999/psid_1").json()
    assert t["bot"]["active"] is False and "another app" in t["bot"]["hold"]
    assert c.post("/api/inbox/messenger/999/psid_1/handback").json() == {"ok": True, "answering_latest": True}
    assert store.get(InboundMessage, m2.id).agent_action == ""  # the skipped message is queued for the bot again
    assert c.get("/api/inbox/messenger/999/psid_1").json()["bot"] == {"active": True, "hold": ""}
    assert store.find(AuditLog, {"event": "dm_handed_back"})
    assert c.post("/api/inbox/messenger/999/nobody/handback").status_code == 404

    m3 = _customer("Which areas do you cover?", "m3", hours_ago=0)
    done = asyncio.run(agent.handle(m3.id, settings=_s(), llm=_answer(fid)))
    assert done.agent_action == "answer"


def test_a_new_person_reply_after_hand_back_keeps_the_bot_out_again():
    _areas()
    _handed_off("Discount?", "m1", hours_ago=1)
    inbox.hand_back("messenger", "999", "psid_1")
    store.insert(InboundMessage(platform="messenger", page_id="999", sender_id="999", recipient_id="psid_1",
                                customer_id="psid_1", mid="m_new", text="Calling you now", is_echo=True,
                                sent_by="human", sent_at=inbox.now()))
    m2 = _customer("ok", "m2", hours_ago=0)
    assert asyncio.run(agent.handle(m2.id, settings=_s(), llm=_llm({}))).agent_action == "skipped"


# --- LOW: FAQ text is sent whole, never cut mid-sentence ---

def test_long_faq_answer_is_sent_whole():  # reviewer's probe (1000-character answer), now passing
    long = "Our deep clean covers kitchen, bathrooms and all rooms. " * 17  # ~950 characters
    f = store.insert(FaqEntry(question="What is included?", answer=long.strip()))
    s = _live(dm_autoreply=True)
    m = _customer("What is included?", "m1")
    client = FakeClient(s)
    done = asyncio.run(agent.handle(m.id, settings=s, llm=_answer(f.id), client=client))
    assert done.agent_action == "answer"
    assert client.posts[0]["message"]["text"].endswith(long.strip())


def test_several_faq_answers_only_whole_entries_that_fit():
    a = FaqEntry(id=1, question="a", answer="A" * 900)
    b = FaqEntry(id=2, question="b", answer="B" * 900)
    c = FaqEntry(id=3, question="c", answer="C" * 100)
    assert agent.faq_text([a, b, c]) == "A" * 900  # b doesn't fit, so stop there (keep the model's order)
    assert agent.faq_text([c, a]) == "C" * 100 + "\n\n" + "A" * 900
    assert agent.faq_text([FaqEntry(id=4, question="d", answer="D" * 1600)]) == ""


def test_faq_text_too_long_to_send_becomes_a_handoff():
    f = store.insert(FaqEntry(question="Everything?", answer="x" * 1600))  # older entry, longer than the API allows
    s = _live(dm_autoreply=True)
    m = _customer("Tell me everything", "m1")
    client = FakeClient(s)
    done = asyncio.run(agent.handle(m.id, settings=s, llm=_answer(f.id), client=client))
    assert done.agent_action == "handoff" and "too long" in done.agent_note
    assert "x" * 50 not in client.posts[0]["message"]["text"]  # the hand-off message, not cut FAQ text


# --- LOW: startup recovery ---

def test_recovery_after_restart_only_drafts_even_with_autoreply(monkeypatch):
    fid = _areas().id
    s = _live(dm_autoreply=True)
    monkeypatch.setattr(webhooks, "get_settings", lambda: s)
    monkeypatch.setattr(agent, "_default_llm", _answer(fid))
    m = _customer("Which areas?", "m1", hours_ago=5)
    sent = []

    async def no_send(*a, **k):
        sent.append(a)
    monkeypatch.setattr(inbox, "send_reply", no_send)
    asyncio.run(webhooks._run_agent([m.id], draft_only=True))
    row = store.get(InboundMessage, m.id)
    assert row.agent_action == "answer" and row.suggested_reply and not sent


def test_startup_keeps_a_reference_to_the_recovery_task(monkeypatch):
    from app import main
    from app.dm import agent as dm_agent

    m = _customer("Which areas?", "m1", hours_ago=1)
    monkeypatch.setenv("DM_AGENT_ENABLED", "true")
    main.get_settings.cache_clear()
    monkeypatch.setattr(dm_agent, "recover_unprocessed", lambda: [m.id])
    calls = []

    async def fake_run(ids, draft_only=False):
        calls.append((ids, draft_only))
    monkeypatch.setattr(webhooks, "_run_agent", fake_run)
    monkeypatch.setattr(main.scheduler, "start", lambda: None)
    monkeypatch.setattr(main.scheduler.scheduler, "shutdown", lambda wait=False: None)
    with TestClient(main.app):
        assert main.app.state.dm_recovery_task is not None
    assert calls == [([m.id], True)]


# --- LOW: idle locks are dropped ---

def test_idle_conversation_locks_are_dropped():
    m = _customer("Which areas?", "m1")
    asyncio.run(agent.handle(m.id, settings=_s(), llm=_llm({"action": "handoff"})))
    assert agent._locks == {}
