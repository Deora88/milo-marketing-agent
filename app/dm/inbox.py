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

from pymongo.errors import DuplicateKeyError

from app.config import Settings, get_settings
from app.db import store
from app.db.models import InboundMessage, now
from app.meta.client import MetaAPIError, MetaClient

STANDARD_WINDOW = timedelta(hours=24)
AD_WINDOW = timedelta(days=7)
HUMAN_AGENT_WINDOW = timedelta(days=7)
MAX_TEXT = 2000  # Messenger text limit
MAX_IG_BYTES = 1000  # Instagram: under 1,000 characters and at most 1,000 bytes (UTF-8; a Hindi letter is 3 bytes)
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


def by_person(m: InboundMessage, settings: Settings | None = None) -> bool:
    """Was this Page message written by a person (not the DM agent)?

    - sent_by "human" / "agent": Milo sent it (Inbox / DM agent).
    - sent_by "": an echo webhook Milo can't match to its own send yet. If its app_id is Milo's app, it is Milo's
      own message whose echo arrived before send_reply stored it (send_reply fills sent_by in a moment).
      Anything else (Business Suite, Instagram app, another tool) counts as a person, which is the safe side:
      the bot steps back. Without META_APP_ID every unmatched echo counts as a person.
    """
    if not m.is_echo or m.sent_by == "agent":
        return False
    if m.sent_by == "human":
        return True
    own_app = (settings or get_settings()).meta_app_id
    return not (own_app and m.app_id == own_app)


def _naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if dt else dt  # MongoDB returns naive UTC, new objects are aware


def _since_hand_back(msgs: list[InboundMessage]) -> list[InboundMessage]:
    """Messages after the last "Hand back to bot" click (all of them if nobody clicked it)."""
    resumed = max((_naive(m.bot_resumed_at) for m in msgs if m.bot_resumed_at), default=None)
    # >=: MongoDB keeps milliseconds only, so a reply in the same millisecond as the click must still count
    return [m for m in msgs if resumed is None or _naive(_when(m)) >= resumed]


def bot_hold(msgs: list[InboundMessage], settings: Settings | None = None, at: datetime | None = None) -> str:
    """Why the DM bot must stay out of this conversation right now, or "" if it may answer.

    - A person replied as the Page (Milo Inbox, Business Suite, Instagram app): until someone clicks
      "Hand back to bot".
    - The bot handed a message to a person (not in the FAQ): for DM_HANDOFF_HOURS after that message, so one
      FAQ miss doesn't silence the bot for this customer for good.
    - A technical failure (Meta timeout, database error) never keeps the bot out.
    """
    s = settings or get_settings()
    at = _naive(at or now())
    live = _since_hand_back(msgs)
    for m in reversed(live):
        if by_person(m, s):
            where = "in the Milo Inbox" if m.sent_by == "human" else f"from another app (app_id {m.app_id or 'none'})"
            return f"a person replied as the Page {where}"
    hold = timedelta(hours=s.dm_handoff_hours)
    for m in reversed(live):
        if not m.is_echo and m.handed_off and at - _naive(_when(m)) < hold:
            left = hold - (at - _naive(_when(m)))
            return f"handed to a person, bot stays out for {max(1, round(left.total_seconds() / 3600))} more hour(s)"
    return ""


def person_owns(msgs: list[InboundMessage], settings: Settings | None = None) -> bool:
    """A person has this conversation right now (see bot_hold)."""
    return bool(bot_hold(msgs, settings))


def hand_back(platform: str, page_id: str, customer_id: str) -> int:
    """A person gives the conversation back to the bot: earlier hand-offs and Page replies stop counting."""
    msgs = [m for m in thread_messages(platform, page_id, customer_id) if not m.is_echo]
    at = now()
    for m in msgs:
        store.update(InboundMessage, m.id, {"handed_off": False, "bot_resumed_at": at})
    if msgs:
        store.audit("dm_handed_back", platform=platform)
    return len(msgs)


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
        live = _since_hand_back(msgs)
        out.append({
            "platform": platform, "page_id": page_id, "customer_id": customer,
            "last_text": last.text, "last_at": _when(last), "last_from": "page" if last.is_echo else "customer",
            "unread": sum(1 for m in msgs if not m.is_echo and m.status == "new"),
            "needs_person": any(m.handed_off or m.agent_action in ("handoff", "failed") for m in live if not m.is_echo)
            and not any(by_person(m) for m in live),
            "messages": len(msgs), "window": reply_window(msgs).as_dict(),
        })
    return sorted(out, key=lambda r: r["last_at"], reverse=True)


def open_thread(platform: str, page_id: str, customer_id: str) -> dict:
    msgs = thread_messages(platform, page_id, customer_id)
    for m in msgs:  # one field only: never overwrite what the agent wrote meanwhile
        if not m.is_echo and m.status == "new" and store.claim(InboundMessage, m.id, {"status": "new"},
                                                                {"status": "read"}):
            m.status = "read"
    hold = bot_hold(msgs)
    return {"platform": platform, "page_id": page_id, "customer_id": customer_id,
            "window": reply_window(msgs).as_dict(), "bot": {"active": not hold, "hold": hold},
            "messages": [m.model_dump() for m in msgs]}


def text_fits(platform: str, text: str) -> bool:
    if platform == "instagram":
        return len(text.encode("utf-8")) < MAX_IG_BYTES
    return len(text) <= MAX_TEXT


_ig_linked: dict[str, str] = {}  # Facebook Page id -> its linked Instagram account id (asked from Meta once)


async def sending_page(client: MetaClient, platform: str, page_id: str) -> str:
    """The Facebook Page that sends (and looks up names) for a conversation.

    Messenger: the Page itself. Instagram (Messenger API for Instagram, via Facebook Login): webhooks carry the
    Instagram account id, but replies go through the linked Facebook Page (META_PAGE_ID) with its Page token.
    The Instagram account must be the one linked to that Page, so a DM to another account is never answered as
    the wrong business."""
    if platform != "instagram":
        return page_id
    s = client.s
    if not s.meta_page_id:
        raise ReplyError("Set META_PAGE_ID (the Facebook Page linked to your Instagram account) to reply to "
                         "Instagram DMs.")
    if s.meta_dry_run:
        return s.meta_page_id
    linked = s.meta_instagram_account_id or _ig_linked.get(s.meta_page_id)
    if not linked:
        try:
            resp = await client.get(f"/{s.meta_page_id}", fields="instagram_business_account")
        except MetaAPIError as exc:
            raise ReplyError(f"Couldn't check which Instagram account is linked to the Page: {exc}") from exc
        linked = str((resp.get("instagram_business_account") or {}).get("id") or "")
        if linked:
            _ig_linked[s.meta_page_id] = linked
    if linked != page_id:
        raise ReplyError(f"This DM went to Instagram account {page_id}, but the Page's linked Instagram account is "
                         f"{linked or 'none'}. Link it in Meta Business Suite or fix META_INSTAGRAM_ACCOUNT_ID.")
    return s.meta_page_id


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
    if not text_fits(platform, text):
        raise ReplyError("Instagram replies must be under 1,000 characters (Hindi letters count as 3)."
                         if platform == "instagram" else f"Replies can be at most {MAX_TEXT} characters.")
    msgs = thread_messages(platform, page_id, customer_id)
    if not msgs:
        raise ReplyError("Conversation not found.")
    win = reply_window(msgs)
    body = {"recipient": {"id": customer_id}, "message": {"text": text}}
    if platform != "instagram":  # Messenger wants a messaging_type; Instagram's Send API takes just these two
        body["messaging_type"] = "RESPONSE"
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
        sender = await sending_page(client, platform, page_id)  # Instagram: the linked Facebook Page
        if s.meta_dry_run:
            resp = await client.post(f"/{sender}/messages", **body)  # logged only
        else:
            token = await _page_token(client, sender)
            resp = await client.post(f"/{sender}/messages", token=token, **body)
    except MetaAPIError as exc:
        raise ReplyError(f"Meta refused the reply: {exc}") from exc
    finally:
        if own:
            await client.aclose()

    mid = resp.get("message_id") or resp.get("id")
    if not mid:  # without an id the reply can't be matched to its echo, and two such replies would collide
        raise ReplyError("Meta accepted the reply but returned no message id. Check the conversation in Meta "
                         "Business Suite before sending again.")
    status = "dry_run" if s.meta_dry_run else "sent"
    try:
        sent = store.insert(InboundMessage(
            platform=platform, page_id=page_id, sender_id=page_id, recipient_id=customer_id, customer_id=customer_id,
            mid=str(mid), text=text, is_echo=True, sent_at=now(), status=status, sent_by=sent_by))
    except DuplicateKeyError:  # Meta's echo webhook got there first: keep its row, record who sent it
        [echo] = store.find(InboundMessage, {"mid": str(mid)})
        sent = store.update(InboundMessage, echo.id, {"sent_by": sent_by, "status": status})
    if not s.meta_dry_run:  # in dry-run nothing reached the person, so their messages stay unanswered
        for m in msgs:
            if not m.is_echo and m.status in ("new", "read"):
                store.claim(InboundMessage, m.id, {"status": {"$in": ["new", "read"]}}, {"status": "answered"})
    store.audit("dm_replied", platform=platform, by=sent_by, tag=body.get("tag", ""), dry_run=s.meta_dry_run)
    return sent
