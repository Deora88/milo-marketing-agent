"""Builds the Meta object tree for a Milo campaign: campaign -> ad set -> (image) -> creative -> ad.

One ad set runs on Facebook and/or Instagram (Meta picks the best placements: Feed, Stories, Reels).
Everything is created PAUSED. Only `set_status()` (called after human approval) sets ACTIVE.
Each step is skipped if its Meta id is already stored, so a publish that failed half-way resumes where it stopped
instead of leaving an empty or duplicate campaign on Meta.
"""
import base64
import html
import re
from pathlib import Path

from app.db.models import Campaign
from app.meta.client import MetaAPIError, MetaClient

DEV_MODE_POST_SUBCODE = 1885183


def meta_name(c: Campaign) -> str:
    """Name used on Meta. The Milo id makes it unique, so a timed-out create can be found again."""
    return f"{c.name} [milo #{c.id}]"


def _id(resp: dict, what: str) -> str:
    if not resp.get("id"):
        raise MetaAPIError(0, None, f"Meta did not return an id for the {what}: {str(resp)[:200]}")
    return str(resp["id"])


async def _find_existing(client: MetaClient, edge: str, name: str) -> str | None:
    """A create that timed out may still have succeeded on Meta. Look it up by its unique name first."""
    if client.s.meta_dry_run:
        return None
    resp = await client.get(f"/{client.account}/{edge}", fields="id,name",
                            filtering=[{"field": "name", "operator": "EQUAL", "value": name}], limit=5)
    rows = resp.get("data") or []
    return str(rows[0]["id"]) if rows else None


async def instagram_user_id(client: MetaClient) -> str:
    """The Instagram account linked to the Page: from .env, or asked from Meta."""
    if client.s.meta_instagram_account_id:
        return client.s.meta_instagram_account_id
    if client.s.meta_dry_run:
        return "dry_instagram"
    resp = await client.get(f"/{client.s.meta_page_id}", fields="instagram_business_account")
    ig = (resp.get("instagram_business_account") or {}).get("id")
    if not ig:
        raise MetaAPIError(0, None, "No Instagram account is linked to the Page. Link one in Meta Business Suite "
                           "(Settings > Accounts > Instagram accounts), set META_INSTAGRAM_ACCOUNT_ID, "
                           "or run this campaign on Facebook only.")
    return str(ig)


async def upload_image(client: MetaClient, path: str) -> str:
    """Upload a local image to the ad account's image library and return its hash."""
    data = Path(path).read_bytes()
    resp = await client.post(f"/{client.account}/adimages", bytes=base64.b64encode(data).decode())
    if client.s.meta_dry_run:
        return "dry_image_hash"
    images = resp.get("images") or {}
    first = next(iter(images.values()), {})
    if not first.get("hash"):
        raise MetaAPIError(0, None, f"Meta did not return an image hash: {str(resp)[:200]}")
    return first["hash"]


async def create_on_meta(client: MetaClient, c: Campaign) -> Campaign:
    acct = client.account
    name = meta_name(c)
    if not c.meta_campaign_id:
        c.meta_campaign_id = await _find_existing(client, "campaigns", name) or _id(await client.post(
            f"/{acct}/campaigns",
            name=name,
            objective=c.objective,
            status="PAUSED",
            special_ad_categories=[] if c.special_ad_category == "NONE" else [c.special_ad_category],
            is_adset_budget_sharing_enabled=False,
        ), "campaign")

    if not c.meta_adset_id:
        c.meta_adset_id = await _find_existing(client, "adsets", f"{name} - ad set") or _id(await client.post(
            f"/{acct}/adsets",
            name=f"{name} - ad set",
            campaign_id=c.meta_campaign_id,
            daily_budget=c.daily_budget,
            billing_event="IMPRESSIONS",
            bid_strategy="LOWEST_COST_WITHOUT_CAP",
            targeting=build_targeting(c),
            status="PAUSED",
            **adset_goal(c, client.s.meta_page_id),
        ), "ad set")

    story_id = existing_post_id(client.s.meta_page_id, client.s.meta_page_post_id)
    if not story_id and c.image_path and not c.meta_image_hash:
        c.meta_image_hash = await upload_image(client, c.image_path)

    if not c.meta_creative_id:
        if story_id:
            # Boost an existing Page post. Its text, image and link come from the post itself.
            c.meta_creative_id = _id(await client.post(f"/{acct}/adcreatives", name=f"{name} - creative",
                                                       object_story_id=story_id), "creative")
        else:
            c.meta_creative_id = _id(await create_link_creative(client, acct, c), "creative")

    if not c.meta_ad_id:
        c.meta_ad_id = await _find_existing(client, "ads", f"{name} - ad") or _id(await client.post(
            f"/{acct}/ads",
            name=f"{name} - ad",
            adset_id=c.meta_adset_id,
            creative={"creative_id": c.meta_creative_id},
            status="PAUSED",
        ), "ad")
    return c


def adset_goal(c: Campaign, page_id: str) -> dict:
    """What the ad set optimises for. Click-to-Messenger: conversations started with the Page."""
    if c.destination == "messenger":
        return {"destination_type": "MESSENGER", "optimization_goal": "CONVERSATIONS",
                "promoted_object": {"page_id": page_id}}
    return {"optimization_goal": "LINK_CLICKS"}


def build_targeting(c: Campaign) -> dict:
    """Ad set targeting. Since Graph API v23 Meta requires an explicit Advantage+ audience choice when a custom age
    range is set (otherwise: error 100 / subcode 1870227). Milo turns it off (0) so the audience is exactly what
    the user approved. publisher_platforms limits delivery to the chosen apps; Meta picks positions within them."""
    if c.radius_km and c.latitude is not None and c.longitude is not None:
        # Local ad: a circle around a point. Countries are left out, because Meta rejects overlapping locations.
        geo = {"custom_locations": [{"latitude": c.latitude, "longitude": c.longitude,
                                     "radius": c.radius_km, "distance_unit": "kilometer"}]}
    else:
        geo = {"countries": c.countries}
    return {
        "geo_locations": geo,
        "age_min": c.age_min,
        "age_max": c.age_max,
        # Click-to-Messenger ads run on Facebook and in Messenger itself.
        "publisher_platforms": ["facebook", "messenger"] if c.destination == "messenger" else list(c.platforms),
        "targeting_automation": {"advantage_audience": 0},
    }


async def set_status(client: MetaClient, c: Campaign, status: str) -> None:
    """status = ACTIVE | PAUSED, applied ad -> ad set -> campaign. If one call fails, the error says which
    objects were already changed, so Meta and Milo can be brought back in line."""
    done: list[str] = []
    for kind, oid in (("ad", c.meta_ad_id), ("ad set", c.meta_adset_id), ("campaign", c.meta_campaign_id)):
        if not oid:
            continue
        try:
            await client.post(f"/{oid}", status=status)
        except MetaAPIError as exc:
            changed = ", ".join(done) or "nothing"
            raise MetaAPIError(exc.code, exc.subcode,
                               f"{exc.message} (setting {kind} to {status}; already changed: {changed})") from exc
        done.append(f"{kind} {oid}")


async def update_budget(client: MetaClient, c: Campaign, daily_budget: int) -> None:
    # Meta allows only 4 budget changes per ad set per hour (subcode 1487632).
    await client.post(f"/{c.meta_adset_id}", daily_budget=daily_budget)


async def story_spec(client: MetaClient, c: Campaign) -> dict:
    """The ad's look, from Milo's draft: Page (+ Instagram account), text, headline, image, link and button."""
    if c.destination == "messenger":
        # Click-to-Messenger: the button opens a chat with the Page, where its automated replies answer.
        link_data = {
            "link": c.link_url or f"https://www.facebook.com/{client.s.meta_page_id}",
            "message": c.primary_text,
            "name": c.headline,
            "call_to_action": {"type": "MESSAGE_PAGE", "value": {"app_destination": "MESSENGER"}},
        }
        if c.welcome_message:
            link_data["page_welcome_message"] = c.welcome_message
    else:
        link_data = {
            "link": c.link_url,
            "message": c.primary_text,
            "name": c.headline,
            "call_to_action": {"type": c.call_to_action, "value": {"link": c.link_url}},
        }
    if c.meta_image_hash:
        link_data["image_hash"] = c.meta_image_hash
    spec = {"page_id": client.s.meta_page_id, "link_data": link_data}
    if "instagram" in c.platforms:
        spec["instagram_user_id"] = await instagram_user_id(client)  # v22+: replaces instagram_actor_id
    return spec


async def create_link_creative(client: MetaClient, acct: str, c: Campaign) -> dict:
    """A new link ad post built from Milo's draft (headline, text, link, button)."""
    try:
        return await client.post(f"/{acct}/adcreatives", name=f"{c.name} - creative",
                                 object_story_spec=await story_spec(client, c))
    except MetaAPIError as exc:
        if exc.subcode == DEV_MODE_POST_SUBCODE:
            raise MetaAPIError(exc.code, exc.subcode, exc.message + " - Meta blocks ad posts created by an app in "
                               "Development mode. Switch the Meta app to Live (App Dashboard > Publish), "
                               "or set META_PAGE_POST_ID to boost an existing Page post.") from exc
        raise


def existing_post_id(page_id: str, raw: str) -> str:
    """Normalise META_PAGE_POST_ID to Meta's "pageid_postid" form. Accepts a bare numeric post id,
    "pageid_postid", or a post URL that contains the numeric id (.../posts/123 or ?story_fbid=123).
    Newer share links carry an opaque "pfbid..." code that the API cannot use, so they are rejected
    with a clear message instead of being sent to Meta."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d+_\d+", raw):
        return raw
    if re.fullmatch(r"\d+", raw):
        return f"{page_id}_{raw}"
    m = re.search(r"(?:story_fbid=|/posts/|/videos/|fbid=)(\d+)(?:\D|$)", raw)
    if m:
        return f"{page_id}_{m.group(1)}"
    raise ValueError("META_PAGE_POST_ID must be the numeric post id (e.g. 1372..._1221...). Links with a "
                     "'pfbid...' code can't be used by the API: get the id from Graph API Explorer with "
                     "<page_id>/posts?fields=id,message")


PREVIEW_FORMATS = {
    "MOBILE_FEED_STANDARD": "Facebook mobile feed",
    "DESKTOP_FEED_STANDARD": "Facebook desktop feed",
    "INSTAGRAM_STANDARD": "Instagram feed",
    "INSTAGRAM_STORY": "Instagram story",
}
_PREVIEW_HOSTS = ("https://www.facebook.com/", "https://business.facebook.com/")


async def get_preview_src(client: MetaClient, ad_id: str, ad_format: str) -> str:
    """Read-only: ask Meta to render the ad (GET /{ad_id}/previews) and return the iframe URL it gives back.
    Only a facebook.com https URL is returned, never Meta's raw HTML."""
    return _iframe_src(await client.get(f"/{ad_id}/previews", ad_format=ad_format))


def _iframe_src(data: dict) -> str:
    body = ((data.get("data") or [{}])[0] or {}).get("body", "")
    m = re.search(r'src="([^"]+)"', body)
    src = html.unescape(m.group(1)) if m else ""
    return src if src.startswith(_PREVIEW_HOSTS) else ""


async def generate_preview_src(client: MetaClient, c: Campaign, ad_format: str) -> str:
    """Read-only: Meta renders the draft (GET /act_x/generatepreviews) without creating any post, creative or
    ad, so the draft can be previewed before it is approved."""
    data = await client.get(f"/{client.account}/generatepreviews", ad_format=ad_format,
                            creative={"object_story_spec": await story_spec(client, c)})
    return _iframe_src(data)
