"""Campaign drafts: one set of rules for creating and editing, the requirements checklist, and version history.

- validate(): Meta's rules and Milo's caps for a draft, shared by create_campaign_draft, update_campaign_draft and
  the UI's edit form. Empty headline, primary text or landing page are allowed in a draft: they show up as
  "missing" and publishing is refused until they are filled in.
- requirements(): every detail a campaign needs, marked known, assumed (Milo filled it in without the user saying
  so, with the reason) or missing. The agent asks about missing and assumed items before requesting publish.
- record_version(): every change to what the ad says, targets or spends is saved as a numbered version with the
  old and new value of each changed field, so "what changed" can be answered at any time.
"""
from app.config import get_settings
from app.db import store
from app.db.models import Campaign, CampaignVersion, now

RESTRICTED = {"HOUSING", "EMPLOYMENT", "CREDIT"}
CATEGORIES = RESTRICTED | {"NONE", "ISSUES_ELECTIONS_POLITICS"}
CTAS = {"LEARN_MORE", "SHOP_NOW", "SIGN_UP", "CONTACT_US", "DOWNLOAD", "BOOK_TRAVEL"}
PLATFORMS = {"facebook", "instagram"}
DESTINATIONS = {"website", "messenger"}

# Campaign fields whose changes are versioned (what the ad says, who sees it, what it spends)
VERSIONED = ["name", "objective", "special_ad_category", "daily_budget", "countries", "location_name", "latitude",
             "longitude", "radius_km", "age_min", "age_max", "platforms", "destination", "headline", "primary_text",
             "link_url", "call_to_action", "welcome_message", "image_path"]
LABELS = {"name": "Name", "objective": "Objective", "special_ad_category": "Special ad category",
          "daily_budget": "Daily budget", "countries": "Countries", "location_name": "Area", "latitude": "Latitude",
          "longitude": "Longitude", "radius_km": "Radius", "age_min": "Minimum age", "age_max": "Maximum age",
          "platforms": "Platforms", "destination": "A click opens", "headline": "Headline",
          "primary_text": "Primary text", "link_url": "Landing page", "call_to_action": "Button",
          "welcome_message": "Welcome message", "image_path": "Ad image"}
# Requirement items, in the order the checklist shows them. Assumptions use these keys.
ITEMS = ["name", "budget", "area", "ages", "platforms", "destination", "special_ad_category", "headline",
         "primary_text", "link_url", "call_to_action", "image"]
ITEM_LABELS = {"name": "Campaign name", "budget": "Daily budget", "area": "Where", "ages": "Ages",
               "platforms": "Platforms", "destination": "A click opens", "special_ad_category": "Special ad category",
               "headline": "Headline", "primary_text": "Primary text", "link_url": "Landing page",
               "call_to_action": "Button", "image": "Ad image"}
# Which requirement item a changed field belongs to (a user-given value clears that item's assumption)
FIELD_ITEM = {"name": "name", "daily_budget": "budget", "countries": "area", "location_name": "area",
              "latitude": "area", "longitude": "area", "radius_km": "area", "age_min": "ages", "age_max": "ages",
              "platforms": "platforms", "destination": "destination", "special_ad_category": "special_ad_category",
              "headline": "headline", "primary_text": "primary_text", "link_url": "link_url",
              "call_to_action": "call_to_action", "image_path": "image", "welcome_message": "destination"}


def _numbers(f: dict, errors: list[str]) -> dict:
    """Numbers as numbers: "500" becomes 500.0, a missing age its default; anything else is an error message
    (a wrong type must never reach a comparison and crash the request)."""
    f = dict(f)
    for key, default, kind in (("daily_budget_inr", None, float), ("age_min", 18, int), ("age_max", 65, int),
                               ("latitude", None, float), ("longitude", None, float), ("radius_km", None, float)):
        v = f.get(key)
        if v is None or v == "":
            f[key] = default
            continue
        try:
            n = float(v)
            if n != n or n in (float("inf"), float("-inf")) or isinstance(v, bool):
                raise ValueError
            f[key] = int(n) if kind is int else n
            if kind is int and n != int(n):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{key} must be a number")
            f[key] = default
    if isinstance(f.get("countries"), str):
        f["countries"] = [f["countries"]]
    return f


def validate(f: dict) -> tuple[list[str], dict, str]:
    """Check a draft given in the tool's terms (rupees, lists). Returns (errors, Campaign fields, extra note)."""
    errors: list[str] = []
    f = _numbers(f, errors)
    name = str(f.get("name") or "").strip()
    if not name or len(name) > 100:
        errors.append("name is required, at most 100 characters")
    radius_km, latitude, longitude = f.get("radius_km"), f.get("latitude"), f.get("longitude")
    countries = [str(c).strip().upper() for c in (f.get("countries") or []) if str(c).strip()]
    if any(len(c) != 2 or not c.isalpha() for c in countries):
        errors.append('countries must be 2-letter ISO codes, e.g. ["IN"]')
    if not countries and radius_km is None:
        errors.append("give at least one country (or local targeting with latitude, longitude and radius_km)")
    budget = f.get("daily_budget_inr")
    if budget is None or budget < 100:
        errors.append("daily budget must be at least Rs 100")
    age_min, age_max = f.get("age_min", 18), f.get("age_max", 65)
    if not 18 <= age_min <= age_max <= 65:
        errors.append("ages must satisfy 18 <= age_min <= age_max <= 65")
    destination = str(f.get("destination") or "website").lower()
    link_url = str(f.get("link_url") or "").strip()
    welcome = str(f.get("welcome_message") or "").strip()
    platforms = f.get("platforms")
    note = ""
    if destination not in DESTINATIONS:
        errors.append("destination must be website or messenger")
    if link_url and not link_url.startswith("https://"):
        errors.append("link_url must start with https://")
    if destination == "messenger":
        if len(welcome) > 300:
            errors.append("welcome_message is longer than 300 characters")
        if platforms and "instagram" in [str(p).lower() for p in platforms]:
            note = " Messenger ads run on Facebook and Messenger, so Instagram was left out."
        platforms = ["facebook"]
    headline, primary = str(f.get("headline") or "").strip(), str(f.get("primary_text") or "").strip()
    if len(headline) > 40:
        errors.append("headline is longer than 40 characters")
    if len(primary) > 125:
        errors.append("primary_text is longer than 125 characters")
    cta = f.get("call_to_action") or "LEARN_MORE"
    if cta not in CTAS:
        errors.append(f"call_to_action must be one of {sorted(CTAS)}")
    category = f.get("special_ad_category") or "NONE"
    if category not in CATEGORIES:
        errors.append(f"special_ad_category must be one of {sorted(CATEGORIES)}")
    if category in RESTRICTED and (age_min != 18 or age_max != 65):
        errors.append(f"{category} ads must target ages 18-65 (Meta rule)")
    platforms = [str(p).lower() for p in (platforms or ["facebook", "instagram"])]
    if not platforms or set(platforms) - PLATFORMS:
        errors.append("platforms must be facebook and/or instagram")
    cap = get_settings().max_daily_budget_inr
    if budget is not None and budget > cap:
        errors.append(f"daily budget is above the Rs {cap:,.0f}/day cap (MAX_DAILY_BUDGET_INR)")
    local = [latitude, longitude, radius_km]
    if any(v is not None for v in local):
        if any(v is None for v in local):
            errors.append("local targeting needs latitude, longitude and radius_km together")
        else:
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                errors.append("latitude/longitude out of range")
            if not 1 <= radius_km <= 80:
                errors.append("radius_km must be between 1 and 80 (Meta rule)")
            if category in RESTRICTED and radius_km < 15:
                errors.append(f"{category} ads need a radius of at least 15 km (Meta rule)")
    if errors:
        return errors, {}, ""
    fields = {
        "name": name, "daily_budget": int(round(budget * 100)), "countries": countries,
        "location_name": str(f.get("location_name") or "").strip(), "latitude": latitude, "longitude": longitude,
        "radius_km": radius_km, "age_min": age_min, "age_max": age_max,
        "platforms": sorted(set(platforms), key=["facebook", "instagram"].index), "destination": destination,
        "headline": headline, "primary_text": primary, "link_url": link_url, "call_to_action": cta,
        "special_ad_category": category, "welcome_message": welcome if destination == "messenger" else "",
        "objective": "OUTCOME_ENGAGEMENT" if destination == "messenger" else "OUTCOME_TRAFFIC",
    }
    return errors, fields, note


def as_tool_fields(c: Campaign) -> dict:
    """A campaign in the tool's terms (rupees), the starting point for an edit."""
    return {"name": c.name, "daily_budget_inr": c.daily_budget / 100, "countries": c.countries,
            "age_min": c.age_min, "age_max": c.age_max, "headline": c.headline, "primary_text": c.primary_text,
            "link_url": c.link_url, "call_to_action": c.call_to_action, "special_ad_category": c.special_ad_category,
            "location_name": c.location_name, "latitude": c.latitude, "longitude": c.longitude,
            "radius_km": c.radius_km, "platforms": c.platforms, "destination": c.destination,
            "welcome_message": c.welcome_message}


# ---------- requirements checklist ----------

def _image_needed(c: Campaign) -> bool:
    return "instagram" in c.platforms and not get_settings().meta_page_post_id and not c.meta_creative_id


def _where(c: Campaign) -> str:
    if c.radius_km:
        return f"{c.radius_km:g} km around {c.location_name or f'{c.latitude}, {c.longitude}'}"
    return ", ".join(c.countries)


def requirements(c: Campaign) -> dict:
    """Each detail: known / assumed (with why) / missing. ready = nothing missing (assumed items are allowed but the
    agent confirms them with the user first)."""
    values = {
        "name": c.name, "budget": f"₹{c.daily_budget / 100:,.0f}/day", "area": _where(c),
        "ages": f"{c.age_min}-{c.age_max}", "platforms": " + ".join(p.title() for p in c.platforms),
        "destination": "Messenger chat" if c.destination == "messenger" else "Landing page",
        "special_ad_category": "None" if c.special_ad_category == "NONE" else c.special_ad_category,
        "headline": c.headline, "primary_text": c.primary_text, "link_url": c.link_url,
        "call_to_action": c.call_to_action, "image": "added" if c.image_path else "",
    }
    missing_now = {
        "headline": not c.headline, "primary_text": not c.primary_text,
        "link_url": c.destination == "website" and not c.link_url,
        "image": _image_needed(c) and not c.image_path,
    }
    items = []
    for key in ITEMS:
        if key == "call_to_action" and c.destination == "messenger":
            continue  # Messenger ads always use "Send message"
        if key == "link_url" and c.destination == "messenger" and not c.link_url:
            continue  # optional for Messenger ads
        if key == "image" and not _image_needed(c) and not c.image_path:
            continue  # optional on Facebook only
        if missing_now.get(key):
            status, note = "missing", {"image": "Instagram needs an image: upload one or generate it",
                                       "link_url": "where a click on the ad goes"}.get(key, "")
        elif key in c.assumptions:
            status, note = "assumed", c.assumptions[key]
        else:
            status, note = "known", ""
        items.append({"key": key, "label": ITEM_LABELS[key], "status": status, "value": values[key], "note": note})
    missing = [i["label"] for i in items if i["status"] == "missing"]
    return {"items": items, "missing": missing, "assumed": [i["label"] for i in items if i["status"] == "assumed"],
            "ready": not missing}


# ---------- versions ----------

def snapshot(c: Campaign) -> dict:
    return {f: getattr(c, f) for f in VERSIONED}


def diff(before: dict, after: dict) -> dict:
    return {f: {"from": before.get(f), "to": after.get(f)} for f in VERSIONED if before.get(f) != after.get(f)}


def record_version(before: dict | None, c: Campaign, source: str, note: str = "") -> CampaignVersion | None:
    """Store version N+1 if anything versioned changed (the first call stores version 1 with no 'from')."""
    after = snapshot(c)
    changes = diff(before, after) if before is not None else {}
    if before is not None and not changes:
        return None
    last = store.find(CampaignVersion, {"campaign_id": c.id}, sort=[("version", -1)], limit=1)
    number = last[0].version + 1 if last else 1
    v = store.insert(CampaignVersion(campaign_id=c.id, version=number, snapshot=after, changes=changes,
                                     source=source, note=note[:300], created_at=now()))
    store.update(Campaign, c.id, {"version": number})
    return v


def show(field: str, value) -> str:
    """A field value in plain words, for 'what changed'."""
    if value in (None, "", []):
        return "(empty)"
    if field == "daily_budget":
        return f"₹{value / 100:,.0f}/day"
    if field == "image_path":
        return "an image"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def describe_changes(changes: dict) -> list[str]:
    out = []
    for f, ch in changes.items():
        if f == "image_path":
            out.append("Ad image: " + ("added" if not ch["from"] else "removed" if not ch["to"] else "replaced"))
        else:
            out.append(f"{LABELS.get(f, f)}: {show(f, ch['from'])} -> {show(f, ch['to'])}")
    return out


def record_image(c_before: Campaign, c_after: Campaign, source: str, how: str) -> CampaignVersion | None:
    """A new ad image is a new version even when it is saved under the same file name."""
    before = snapshot(c_before)
    if before["image_path"] == c_after.image_path:
        before["image_path"] = f"{c_before.image_path}#previous" if c_before.image_path else ""
    return record_version(before, c_after, source, how)


def history(campaign_id: int) -> list[dict]:
    rows = store.find(CampaignVersion, {"campaign_id": campaign_id}, sort=[("version", -1)])
    return [{"version": v.version, "at": v.created_at, "source": v.source, "note": v.note,
             "changes": describe_changes(v.changes), "fields": list(v.changes)} for v in rows]


# ---------- editing a draft ----------

class DraftError(ValueError):
    """An edit Milo refuses; the message is shown to the user or the agent."""


def update_draft(campaign_id: int, changes: dict, source: str, assumed: dict | None = None,
                 confirmed: list[str] | None = None, note: str = "") -> tuple[Campaign, list[str], str]:
    """Apply changes (tool terms) to a draft. Returns (campaign, plain-language changes, extra note).
    Only drafts can be edited: a waiting publish request must be rejected first, and a live ad changes only
    through approvals (budget, pause)."""
    c = store.get(Campaign, campaign_id)
    if c is None:
        raise DraftError("Campaign not found.")
    if c.status != "draft":
        raise DraftError({"pending_approval": "A publish request for this campaign is waiting. Reject it first, "
                                              "then edit the draft.",
                          }.get(c.status, f"This campaign is {c.status}: its ad can't be edited. Budget and pause "
                                          "changes go through approvals."))
    unknown = set(changes) - set(as_tool_fields(c))
    if unknown:
        raise DraftError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
    merged = {**as_tool_fields(c), **changes}
    errors, fields, extra = validate(merged)
    if errors:
        raise DraftError("; ".join(errors))
    before = snapshot(c)
    assumptions = dict(c.assumptions)
    for f in changes:  # a value given now is known, unless it is given as an assumption again
        assumptions.pop(FIELD_ITEM.get({"daily_budget_inr": "daily_budget"}.get(f, f), f), None)
    for key in confirmed or []:
        assumptions.pop(key, None)
    for key, why in (assumed or {}).items():
        if key in ITEMS:
            assumptions[key] = str(why)[:200]
    updated = store.update(Campaign, c.id, {**fields, "assumptions": assumptions})
    v = record_version(before, updated, source, note)
    if v is None and assumptions != c.assumptions:
        store.audit("draft_confirmed", campaign_id=c.id, source=source, items=sorted(set(c.assumptions) - set(assumptions)))
    elif v is not None:
        store.audit("draft_updated", campaign_id=c.id, source=source, version=v.version, fields=list(v.changes))
    return store.get(Campaign, c.id), describe_changes(v.changes) if v else [], extra
