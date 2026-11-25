"""Sign-in API. /state, /login, /setup and /logout work without a session; /users needs an admin
(enforced in app/security.py, like every role rule)."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import auth
from app.config import get_settings
from app.db import store
from app.db.models import User
from app.security import require_admin

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class SetupIn(LoginIn):
    name: str = Field("", max_length=80)


class UserIn(SetupIn):
    role: Literal["editor", "admin"] = "editor"


class UserPatch(BaseModel):
    role: Optional[Literal["editor", "admin"]] = None
    disabled: Optional[bool] = None
    password: Optional[str] = Field(None, max_length=200)
    name: Optional[str] = Field(None, max_length=80)


def _set_cookie(response: Response, request: Request, sid: str) -> None:
    response.set_cookie(auth.COOKIE, sid, max_age=get_settings().session_hours * 3600, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.get("/state")
def state(request: Request):
    """What the sign-in page needs: is sign-in on, does the first admin still have to be created, who am I."""
    s = get_settings()
    if not s.auth_required:
        return {"auth_required": False, "setup_needed": False, "user": None}
    u = auth.user_for(request.cookies.get(auth.COOKIE, ""))
    return {"auth_required": True, "setup_needed": u is None and auth.setup_needed(),
            "user": auth.public(u) if u else None}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response):
    try:
        sid, u = auth.login(body.email, body.password, _ip(request))
    except auth.AuthError as exc:
        raise HTTPException(429 if "locked" in str(exc) else 401, str(exc)) from exc
    _set_cookie(response, request, sid)
    return {"user": auth.public(u)}


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response):
    """First run only: create the first admin and sign in. Refused once any user exists."""
    try:
        auth.create_first_admin(body.email, body.password, body.name)
        sid, u = auth.login(body.email, body.password, _ip(request))
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    _set_cookie(response, request, sid)
    return {"user": auth.public(u)}


@router.post("/logout")
def logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE, ""))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/users", dependencies=[Depends(require_admin)])
def users():
    return [auth.public(u) for u in store.find(User, sort=[("_id", 1)])]


@router.post("/users", dependencies=[Depends(require_admin)])
def add_user(body: UserIn):
    try:
        return auth.public(auth.create_user(body.email, body.password, body.role, body.name))
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/users/{user_id}", dependencies=[Depends(require_admin)])
def change_user(user_id: int, body: UserPatch):
    try:
        return auth.public(auth.update_user(user_id, **body.model_dump(exclude_none=True)))
    except auth.AuthError as exc:
        raise HTTPException(404 if "not found" in str(exc) else 409, str(exc)) from exc
