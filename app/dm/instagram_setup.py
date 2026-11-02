"""Read-only check of everything Instagram DMs need on Meta's side (Messenger API for Instagram, via the Page).

Nothing here changes anything on Meta. Each check says what is wrong and where to fix it:
1. an Instagram professional account is linked to the Facebook Page (META_PAGE_ID);
2. the Page is subscribed to this app for messages, postbacks and echoes;
3. the app's webhook is subscribed to the "instagram" object with the messages field (App Dashboard);
4. Milo has received an Instagram DM (proof that 1-3 and the Instagram setting below all work).
Two things can't be read through the API and are listed as reminders: the Instagram app setting
"Allow access to messages", and that the Meta app is published (Live) - Meta sends no webhooks otherwise.
"""
from app.config import Settings, get_settings
from app.db import store
from app.db.models import InboundMessage
from app.dm import inbox
from app.meta.client import MetaAPIError, MetaClient
from app.util.redact import redact

PAGE_FIELDS = ("messages", "messaging_postbacks", "message_echoes")
MANUAL = [
    "In the Instagram app: Settings > Messages and story replies > Message controls > Connected tools > turn on "
    "\"Allow access to messages\".",
    "The Meta app must be published (Live) to receive webhooks. With Standard Access, only people with a role on "
    "the app can DM and get replies; everyone else needs Advanced Access for instagram_manage_messages (App Review).",
    "The token needs instagram_basic, instagram_manage_messages, pages_manage_metadata and pages_messaging.",
]


def _err(exc: Exception) -> str:
    return redact(exc)[:200]


async def check(client: MetaClient | None = None, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    checks: list[dict] = []

    def add(key: str, label: str, ok: bool | None, detail: str) -> None:
        checks.append({"key": key, "label": label, "ok": ok, "detail": detail})

    last = store.find(InboundMessage, {"platform": "instagram", "is_echo": False}, sort=[("_id", -1)], limit=1)
    received = (f"Last Instagram DM received {last[0].received_at:%Y-%m-%d %H:%M} UTC." if last
                else "No Instagram DM received yet. Send one from another Instagram account to test.")
    if s.meta_dry_run:
        add("received", "Instagram DMs received", bool(last), received)
        return {"dry_run": True, "ok": False, "checks": checks, "manual": MANUAL,
                "note": "Dry run is on (META_DRY_RUN=true): Meta isn't asked anything."}
    if not s.meta_page_id:
        add("page", "Facebook Page", False, "Set META_PAGE_ID: Instagram DMs are answered through the Page linked "
                                            "to your Instagram account.")
        return {"dry_run": False, "ok": False, "checks": checks, "manual": MANUAL}

    own = client is None
    client = client or MetaClient(s)
    try:
        # 1. Instagram account linked to the Page
        try:
            resp = await client.get(f"/{s.meta_page_id}", fields="instagram_business_account{id,username}")
            ig = resp.get("instagram_business_account") or {}
            if not ig.get("id"):
                add("linked", "Instagram account linked to the Page", False,
                    "No Instagram professional account is linked to this Page. Link it in Meta Business Suite > "
                    "Settings > Accounts > Instagram accounts.")
            elif s.meta_instagram_account_id and s.meta_instagram_account_id != str(ig["id"]):
                add("linked", "Instagram account linked to the Page", False,
                    f"The Page is linked to @{ig.get('username', '?')} ({ig['id']}), but META_INSTAGRAM_ACCOUNT_ID "
                    f"is {s.meta_instagram_account_id}. Fix or remove META_INSTAGRAM_ACCOUNT_ID.")
            else:
                add("linked", "Instagram account linked to the Page", True,
                    f"@{ig.get('username', '?')} (id {ig['id']}) is linked to Page {s.meta_page_id}.")
        except MetaAPIError as exc:
            add("linked", "Instagram account linked to the Page", False, f"Meta refused: {_err(exc)}")

        # 2. Page subscribed to this app for messaging events
        try:
            token = await inbox._page_token(client, s.meta_page_id)
            subs = (await client.get(f"/{s.meta_page_id}/subscribed_apps", token=token)).get("data") or []
            mine = [a for a in subs if not s.meta_app_id or str(a.get("id")) == s.meta_app_id]
            have = set(mine[0].get("subscribed_fields") or []) if mine else set()
            missing = [f for f in PAGE_FIELDS if f not in have]
            add("page_subscription", "Page subscribed to the app", not missing,
                "Subscribed to " + ", ".join(sorted(have)) + "." if not missing else
                ("The Page is not subscribed to this app." if not mine else
                 f"Missing field(s): {', '.join(missing)}.") +
                " Fix: App Dashboard > Messenger > Settings (or Webhooks) > the Page > Add subscriptions.")
        except (MetaAPIError, inbox.ReplyError) as exc:
            add("page_subscription", "Page subscribed to the app", False, f"Couldn't check: {_err(exc)}")

        # 3. App webhook subscribed to the "instagram" object (needs the app id and secret: app access token)
        if s.meta_app_id and s.meta_app_secret:
            try:
                rows = (await client.get(f"/{s.meta_app_id}/subscriptions",
                                         token=f"{s.meta_app_id}|{s.meta_app_secret}")).get("data") or []
                obj = next((r for r in rows if r.get("object") == "instagram"), None)
                fields = {f.get("name") for f in (obj or {}).get("fields") or []}
                if obj and obj.get("active", True) and "messages" in fields:
                    add("app_webhook", "App webhook for Instagram", True,
                        f"Subscribed to the instagram object ({', '.join(sorted(fields))}) at "
                        f"{obj.get('callback_url', '?')}.")
                else:
                    add("app_webhook", "App webhook for Instagram", False,
                        ("The app webhook has no instagram object." if not obj else
                         "The instagram object is not subscribed to the messages field.") +
                        " Fix: App Dashboard > Webhooks > choose Instagram > Callback URL = your /webhooks/meta "
                        "address and verify token > subscribe to messages and messaging_postbacks.")
            except MetaAPIError as exc:
                add("app_webhook", "App webhook for Instagram", False, f"Couldn't check: {_err(exc)}")
        else:
            add("app_webhook", "App webhook for Instagram", None,
                "Set META_APP_ID and META_APP_SECRET so Milo can check this; otherwise check App Dashboard > "
                "Webhooks > Instagram yourself.")

        # 4. Proof it works end to end
        add("received", "Instagram DMs received", bool(last), received)
    finally:
        if own:
            await client.aclose()
    return {"dry_run": False, "ok": all(c["ok"] for c in checks if c["ok"] is not None), "checks": checks,
            "manual": MANUAL}
