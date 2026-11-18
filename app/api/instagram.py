"""Instagram DMs (localhost only, like every /api route): read-only check of the Meta-side setup."""
from fastapi import APIRouter

from app.dm import instagram_setup

router = APIRouter(prefix="/api/instagram", tags=["dm agent"])


@router.get("/check")
async def check():
    """What Instagram DMs need on Meta's side, and what is missing. Changes nothing on Meta."""
    return await instagram_setup.check()
