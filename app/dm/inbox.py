"""DM inbox: messages grouped into conversations, Meta's reply window, and human replies through the Send API.

Step 2 of DM handling. A person (in the Milo Inbox) reads and answers; the DM agent (step 3) will reuse the same
`send_reply` with sent_by="agent". Nothing here can change an ad.

Meta's messaging rules (developers.facebook.com, Messenger Platform policy):
- Standard window: reply within 24 hours of the person's last message.
- A person's first message after clicking a click-to-Messenger / click-to-Direct ad opens a window of up to 7 days.
- After the window, only a human may reply, with the HUMAN_AGENT tag, up to 7 days after the person's last
  message, and only if the app has Meta's Human Agent feature (META_HUMAN_AGENT_ENABLED=true).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import Settings, get_settings
from app.db import store
from app.db.models import InboundMessage, now
from app.meta.client import MetaAPIError, MetaClient

STANDARD_WINDOW = timedelta(hours=24)
AD_WINDOW = timedelta(days=7)
HUMAN_AGENT_WINDOW = timedelta(days=7)
MAX_TEXT = 2000  # Messenger text limit
_page_tokens: dict[str, str] = {}


class ReplyError(ValueError):
    """A reply that Milo refuses to send (window closed, empty text...). Message is shown to the user."""


@dataclass
class Window:
    state: str  # open | human_agent | closed
    closes_at: datetime | None
    human_agent_until: datetime | None
    from_ad: bool

    def as_dict(self) -> dict:
        return {"state": self.state, "closes_at": self.closes_at, "human_agent_until": self.human_agent_until,
                "from_ad": self.from_ad}


def _when(m: InboundMessage) -> datetime:
    return m.sent_at or m.received_at


def _from_ad(m: InboundMessage) -> bool:
    r = m.referral or {}
    return bool(r.get("ad_id")) or str(r.get("source", "")).upper() == "ADS"


def reply_window(messages: list[InboundMessage], at: datetime | None = None) -> Window:
    """Can the Page still reply, and how? Based on the person's own messages only."""
    at = at or now()
    inbound = sorted((m for m in messages if not m.is_echo), key=_when)
    if not inbound:
        return Window("closed", None, None, False)
    last = _when(inbound[-1])
    closes = last + STANDARD_WINDOW
    ad_msgs = [m for m in inbound if _from_ad(m)]
    if ad_msgs:
        closes = max(closes, _when(ad_msgs[0]) + AD_WINDOW)
    human_until = last + HUMAN_AGENT_WINDOW
    state = "open" if at < closes else "human_agent" if at < human_until else "closed"
    return Window(state, closes, human_until, bool(ad_msgs))


def _key(m: InboundMessage) -> tuple[str, str, str]:
    customer = m.customer_id or (m.recipient_id if m.is_echo else m.sender_id)
    return (m.platform, m.page_id, customer)


def thread_messages(platform: str, page_id: str, customer_id: str) -> list[InboundMessage]:
    rows = store.find(InboundMessage, {"platform": platform, "page_id": page_id})
    return sorted((m for m in rows if _key(m)[2] == customer_id), key=_when)


def list_conversations() -> list[dict]:
    """One row per (platform, Page, person), newest activity first."""
    groups: dict[tuple, list[InboundMessage]] = {}
    for m in store.find(InboundMessage):
        groups.setdefault(_key(m), []).append(m)
    out = []
    for (platform, page_id, customer), msgs in groups.items():
        msgs.sort(key=_when)
        last = msgs[-1]
        out.append({
            "platform": platform, "page_id": page_id, "customer_id": customer,
            "last_text": last.text, "last_at": _when(last), "last_from": "page" if last.is_echo else "customer",
            "unread": sum(1 for m in msgs if not m.is_echo and m.status == "new"),
            "needs_person": any(m.handed_off for m in msgs if not m.is_echo)
            and not any(m.is_echo and m.sent_by == "human" for m in msgs),
            "messages": len(msgs), "window": reply_window(msgs).as_dict(),
        })
    return sorted(out, key=lambda r: r["last_at"], reverse=True)


def open_thread(platform: str, page_id: str, customer_id: str) -> dict:
    msgs = thread_messages(platform, page_id, customer_id)
    for m in msgs:
        if not m.is_echo and m.status == "new":
            m.status = "read"
            store.save(m)
    return {"platform": platform, "page_id": page_id, "customer_id": customer_id,
            "window": reply_window(msgs).as_dict(), "messages": [m.model_dump() for m in msgs]}


async def _page_token(client: MetaClient, page_id: str) -> str:
    s = client.s
    if s.meta_page_access_token:
        return s.meta_page_access_token
    if page_id not in _page_tokens:
        resp = await client.get(f"/{page_id}", fields="access_token")
        if not resp.get("access_token"):
            raise ReplyError("Couldn't get a Page access token. Set META_PAGE_ACCESS_TOKEN, or give META_ACCESS_TOKEN "
                             "access to the Page with pages_messaging.")
        _page_tokens[page_id] = resp["access_token"]
    return _page_tokens[page_id]


async def send_reply(platform: str, page_id: str, customer_id: str, text: str, sent_by: str = "human",
                     client: MetaClient | None = None, settings: Settings | None = None) -> InboundMessage:
    """Send one text reply through Meta's Send API, inside the rules above, and store it in the thread."""
    s = settings or get_settings()
    text = (text or "").strip()
    if not text:
        raise ReplyError("Write a reply first.")
    if len(text) > MAX_TEXT:
        raise ReplyError(f"Replies can be at most {MAX_TEXT} characters.")
    msgs = thread_messages(platform, page_id, customer_id)
    if not msgs:
        raise ReplyError("Conversation not found.")
    win = reply_window(msgs)
    body = {"recipient": {"id": customer_id}, "message": {"text": text}, "messaging_type": "RESPONSE"}
    if win.state == "human_agent":
        if sent_by != "human" or not s.meta_human_agent_enabled:
            raise ReplyError("The 24-hour reply window has closed. Meta allows a late reply only from a person with "
                             "the HUMAN_AGENT tag, which needs the Human Agent feature (META_HUMAN_AGENT_ENABLED).")
        body.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
    elif win.state == "closed":
        raise ReplyError("The reply window has closed. Meta doesn't allow replies to this person until they "
                         "message the Page again.")

    own = client is None
    client = client or MetaClient(s)
    try:
        if s.meta_dry_run:
            resp = await client.post(f"/{page_id}/messages", **body)  # logged only
        else:
            token = await _page_token(client, page_id)
            resp = await client.post(f"/{page_id}/messages", token=token, **body)
    except MetaAPIError as exc:
        raise ReplyError(f"Meta refused the reply: {exc}") from exc
    finally:
        if own:
            await client.aclose()

    sent = store.insert(InboundMessage(
        platform=platform, page_id=page_id, sender_id=page_id, recipient_id=customer_id, customer_id=customer_id,
        mid=str(resp.get("message_id") or resp.get("id")), text=text, is_echo=True, sent_at=now(),
        status="dry_run" if s.meta_dry_run else "sent", sent_by=sent_by))
    for m in msgs:
        if not m.is_echo and m.status in ("new", "read"):
            m.status = "answered"
            store.save(m)
    store.audit("dm_replied", platform=platform, by=sent_by, tag=body.get("tag", ""), dry_run=s.meta_dry_run)
    return sent
