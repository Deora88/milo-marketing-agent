"""A hanging model or Meta call must never leave a DM stuck on "working on it"."""
import asyncio
import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app.db import store
from app.db.models import AuditLog, InboundMessage, now
from app.dm import agent
from app.main import app
from tests.conftest import WEB
from tests.test_dm_hardening import _areas, _customer, _live, _s


def test_model_that_never_answers_becomes_a_handoff(monkeypatch):  # the live bug: openrouter/free hung
    monkeypatch.setattr(agent, "MODEL_TIMEOUT", 0.05)

    async def hangs(messages):
        await asyncio.sleep(3600)
    m = _customer("How much does a car wash cost?", "m1")
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=hangs))
    assert done.agent_action == "handoff" and "no reply within" in done.agent_note
    assert store.find(AuditLog, {"event": "dm_model_error"})


def test_whole_run_has_a_hard_limit(monkeypatch):
    monkeypatch.setattr(agent, "RUN_TIMEOUT", 0.1)
    fid = _areas().id
    s = _live(dm_autoreply=True)

    class SlowMeta:
        def __init__(self):
            self.s = s

        async def get(self, *a, **k):
            return {"access_token": "PT"}

        async def post(self, *a, **k):
            await asyncio.sleep(3600)

        async def aclose(self):
            pass

    async def llm(messages):
        return json.dumps({"action": "answer", "faq_ids": [fid], "reply": "x"})
    m = _customer("Which areas?", "m1")
    done = asyncio.run(agent.handle(m.id, settings=s, llm=llm, client=SlowMeta()))
    assert done.agent_action == "failed" and "took longer" in done.agent_note
    assert agent._locks == {}  # the lock was released


def test_stuck_processing_rows_are_failed_and_can_be_redrafted(monkeypatch):
    async def no_model(messages):
        return json.dumps({"action": "handoff"})
    monkeypatch.setattr(agent, "_default_llm", no_model)
    old = _customer("How much does a car wash cost?", "m1")
    store.update(InboundMessage, old.id, {"agent_action": "processing",
                                          "agent_started_at": now() - timedelta(minutes=14)})
    fresh = _customer("Hello?", "m2", customer="psid_2")
    store.update(InboundMessage, fresh.id, {"agent_action": "processing", "agent_started_at": now()})
    assert agent.fail_stuck() == [old.id]
    assert store.get(InboundMessage, old.id).agent_action == "failed"
    assert store.get(InboundMessage, fresh.id).agent_action == "processing"  # still running, left alone
    r = TestClient(app, headers=WEB).post("/api/inbox/messenger/999/psid_1/suggest")
    assert r.status_code == 200  # "Ask the bot for a draft" works again (was 409 while stuck)


def test_model_timeout_does_not_keep_the_bot_out_of_the_chat(monkeypatch):  # the live bug, second half
    from app.db.models import Lead

    monkeypatch.setattr(agent, "MODEL_TIMEOUT", 0.05)
    fid = _areas().id

    async def hangs(messages):
        await asyncio.sleep(3600)
    first = _customer("How much does a car wash cost?", "m1", hours_ago=0.3)
    done = asyncio.run(agent.handle(first.id, settings=_s(), llm=hangs))
    assert done.agent_action == "handoff" and not done.handed_off  # goes to a person, no 24-hour hold
    from app.dm import inbox
    assert inbox.list_conversations()[0]["needs_person"] is True

    async def answers(messages):
        return json.dumps({"action": "answer", "faq_ids": [fid], "reply": "We cover Delhi NCR."})
    second = _customer("Which areas do you cover?", "m2", hours_ago=0)
    assert asyncio.run(agent.handle(second.id, settings=_s(), llm=answers)).agent_action == "answer"
    assert not store.find(Lead)


def test_phone_number_is_saved_even_while_a_person_handles_the_chat():
    from app.db.models import Lead

    held = _customer("Can I get a discount?", "m1", hours_ago=1)
    store.update(InboundMessage, held.id, {"agent_action": "handoff", "handed_off": True})
    m = _customer("How much does a car wash cost? 9871701615", "m2", hours_ago=0)
    done = asyncio.run(agent.handle(m.id, settings=_s(), llm=None))
    assert done.agent_action == "skipped"
    assert [x.phone for x in store.find(Lead)] == ["9871701615"]
