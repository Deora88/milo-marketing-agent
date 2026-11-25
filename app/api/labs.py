"""Trends page API (prototype; code name "labs"; localhost only like every /api route). Read-only except the competitor list
(and the hashtag cache).
All routes answer 404 when LABS_ENABLED=false."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import labs
from app.config import get_settings
from app.labs import competitors, hashtags, moments, own


def _enabled():
    if not get_settings().labs_enabled:
        raise HTTPException(404, "The Trends page is turned off (LABS_ENABLED=false).")


router = APIRouter(prefix="/api/labs", tags=["labs"], dependencies=[Depends(_enabled)])


class CompetitorIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)


def _err(exc: competitors.LabsError) -> HTTPException:
    return HTTPException(404 if "not on the list" in str(exc) else 409, str(exc))


@router.get("")
def overview(keyword: str = ""):
    """Everything the Labs page shows: own-account patterns, upcoming moments, competitors, Ad Library link."""
    s = get_settings()
    kw = keyword.strip() or s.dm_business_name
    return {"version": labs.VERSION, "own": own.summary(), "moments": moments.upcoming(business=s.dm_business_name),
            "competitors": competitors.overview(),
            "hashtags": {"default": s.labs_default_hashtag, "live_enabled": hashtags.is_live(),
                         "used": hashtags.used_this_week(), "limit": hashtags.WEEKLY_LIMIT,
                         "requirement": hashtags.NEEDS},
            "ad_library": {"keyword": kw, "country": s.labs_ad_library_country, "url": labs.ad_library_url(kw)}}


@router.post("/competitors")
async def add_competitor(body: CompetitorIn):
    try:
        c = competitors.add(body.username)
    except competitors.LabsError as exc:
        raise _err(exc) from exc
    await competitors.refresh(c, force=True)
    return competitors.overview()


@router.delete("/competitors/{username}")
def remove_competitor(username: str):
    try:
        competitors.remove(username)
    except competitors.LabsError as exc:
        raise _err(exc) from exc
    return competitors.overview()


@router.post("/competitors/refresh")
async def refresh_competitors():
    await competitors.refresh_all(force=True)
    return competitors.overview()


class HashtagIn(BaseModel):
    tag: str = Field("", max_length=101)
    refresh: bool = False


@router.post("/hashtags")
async def hashtag(body: HashtagIn):
    """Top and recent public posts for one hashtag. Sample posts until Meta approves Instagram Public Content Access.
    POST, not GET: a new hashtag uses one of Instagram's 30 a week, so it needs the X-Milo-Client header (a link or
    image on another website can't trigger a GET search and spend the allowance)."""
    try:
        return await hashtags.lookup(body.tag or get_settings().labs_default_hashtag, force=body.refresh)
    except competitors.LabsError as exc:
        raise HTTPException(409, str(exc)) from exc
