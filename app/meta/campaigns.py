"""Builds the Meta object tree for a Milo campaign: campaign -> ad set -> creative -> ad.

Everything is created PAUSED. Only `set_status()` (called after human approval) sets ACTIVE.
Each step is skipped if its Meta id is already stored, so a publish that failed half-way resumes where it stopped
instead of leaving an empty campaign on Meta.
"""
import html
import re

from app.db.models import Campaign
from app.meta.client import MetaAPIError, MetaClient

DEV_MODE_POST_SUBCODE = 1885183


async def create_on_meta(client: MetaClient, c: Campaign) -> Campaign:
    acct = client.account
    if not c.meta_campaign_id:
        camp = await client.post(
            f"/{acct}/campaigns",
            name=c.name,
            objective=c.objective,
            status="PAUSED",
            special_ad_categories=[] if c.special_ad_category == "NONE" else [c.special_ad_category],
            is_adset_budget_sharing_enabled=False,
        )
        c.meta_campaign_id = camp["id"]

    if not c.meta_adset_id:
        adset = await client.post(
            f"/{acct}/adsets",
            name=f"{c.name} - ad set",
            campaign_id=c.meta_campaign_id,
            daily_budget=c.daily_budget,
            billing_event="IMPRESSIONS",
            optimization_goal="LINK_CLICKS",
            bid_strategy="LOWEST_COST_WITHOUT_CAP",
            targeting=build_targeting(c),
            status="PAUSED",
        )
        c.meta_adset_id = adset["id"]

    if not c.meta_creative_id:
        story_id = existing_post_id(client.s.meta_page_id, client.s.meta_page_post_id)
        if story_id:
            # Boost an existing Page post. Its text, image and link come from the post itself.
            creative = await client.post(f"/{acct}/adcreatives", name=f"{c.name} - creative",
                                         object_story_id=story_id)
        else:
            creative = await create_link_creative(client, acct, c)
        c.meta_creative_id = creative["id"]

    if not c.meta_ad_id:
        ad = await client.post(
            f"/{acct}/ads",
            name=f"{c.name} - ad",
            adset_id=c.meta_adset_id,
            creative={"creative_id": c.meta_creative_id},
            status="PAUSED",
        )
        c.meta_ad_id = ad["id"]
    return c


def build_targeting(c: Campaign) -> dict:
    """Ad set targeting. Since Graph API v23 Meta requires an explicit Advantage+ audience choice when a custom age
    range is set (otherwise: error 100 / subcode 1870227). Milo turns it off (0) so the audience is exactly what
    the user approved."""
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
        "targeting_automation": {"advantage_audience": 0},
    }


async def set_status(client: MetaClient, c: Campaign, status: str) -> None:
    """status = ACTIVE | PAUSED. Applied to the campaign (children inherit delivery)."""
    if c.meta_ad_id:
        await client.post(f"/{c.meta_ad_id}", status=status)
    if c.meta_adset_id:
        await client.post(f"/{c.meta_adset_id}", status=status)
    await client.post(f"/{c.meta_campaign_id}", status=status)


async def update_budget(client: MetaClient, c: Campaign, daily_budget: int) -> None:
    # Meta allows only 4 budget changes per ad set per hour (subcode 1487632).
    await client.post(f"/{c.meta_adset_id}", daily_budget=daily_budget)


def story_spec(client: MetaClient, c: Campaign) -> dict:
    """The ad's look, from Milo's draft: Page, text, headline, link and button."""
    return {
        "page_id": client.s.meta_page_id,
        "link_data": {
            "link": c.link_url,
            "message": c.primary_text,
            "name": c.headline,
            "call_to_action": {"type": c.call_to_action, "value": {"link": c.link_url}},
        },
    }


async def create_link_creative(client: MetaClient, acct: str, c: Campaign) -> dict:
    """A new link ad post built from Milo's draft (headline, text, link, button)."""
    try:
        return await client.post(f"/{acct}/adcreatives", name=f"{c.name} - creative",
                                 object_story_spec=story_spec(client, c))
    except MetaAPIError as exc:
        if exc.subcode == DEV_MODE_POST_SUBCODE:
            raise MetaAPIError(exc.code, exc.subcode, exc.message + " - Meta blocks ad posts created by an app in "
                               "Development mode. Make a post on your Page and set META_PAGE_POST_ID in .env, "
                               "or switch the Meta app to Live.") from exc
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
    ad. Works before publishing, and while Meta blocks ad posts from a Development-mode app."""
    data = await client.get(f"/{client.account}/generatepreviews", ad_format=ad_format,
                            creative={"object_story_spec": story_spec(client, c)})
    return _iframe_src(data)
