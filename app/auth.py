"""Sign-in and roles (AUTH_REQUIRED=true, the default).

- Users live in MongoDB (collection users): email, name, role, and a salted scrypt hash of the password. The
  password itself is never stored or logged.
- Roles: "editor" can do everything except approve / reject and manage users; "admin" can do everything.
  The check is on the server (app/security.py); hiding a button in the UI is only cosmetic.
- A session is a random 256-bit id in an HttpOnly, SameSite=Strict cookie. Only its SHA-256 is stored, so a
  copy of the database cannot be used to sign in. Sessions end after SESSION_HOURS, on sign-out, or when an
  admin disables the user or resets the password.
- Sign-in is slowed down: MAX_FAILS wrong tries for one email (or from one public remote address) lock it for
  LOCK_MINUTES. Loopback and private addresses are skipped for the per-address lock: every local and tunnelled
  user shares 127.0.0.1, and under Docker every request arrives from the bridge address (172.x).
- First run: while no user exists, the sign-in page offers "create the first admin". It is reachable only from
  this computer (the tunnel guard in app/security.py blocks /api through ngrok) and stops working as soon as one
  user exists.
"""
import hashlib
import ipaddress
import hmac
import re
import secrets
import time
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.db import store
from app.db.models import Session, User, now

COOKIE = "milo_session"
ROLES = ("editor", "admin")
MIN_PASSWORD = 10
MAX_FAILS = 5
LOCK_MINUTES = 15
CACHE_SECONDS = 60  # a signed-in request re-checks the database at most this often
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_DUMMY_SALT = secrets.token_bytes(16)

_fails: dict[str, list[float]] = {}  # "email:<x>" / "ip:<x>" -> times of recent wrong tries (this process)
_cache: dict[str, tuple[float, User]] = {}  # sid hash -> (checked at, user)


class AuthError(ValueError):
    """A sign-in or user-management request Milo refuses; the message is shown to the user."""


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def _sid_hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def _clean_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL.match(e):
        raise AuthError("Enter a valid email address.")
    return e


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Use a password of at least {MIN_PASSWORD} characters.")
    if len(password) > 200:
        raise AuthError("Use a password of at most 200 characters.")


def public(u: User) -> dict:
    """What the browser may know about a user: never the hash or salt."""
    return {"id": u.id, "email": u.email, "name": u.name or u.email.split("@")[0], "role": u.role,
            "disabled": u.disabled, "created_at": u.created_at, "last_login_at": u.last_login_at}


def setup_needed() -> bool:
    return store.count(User) == 0


def create_user(email: str, password: str, role: str = "editor", name: str = "") -> User:
    e = _clean_email(email)
    _check_password(password)
    if role not in ROLES:
        raise AuthError(f"Role must be one of: {', '.join(ROLES)}.")
    salt = secrets.token_bytes(16)
    try:
        u = store.insert(User(email=e, name=(name or "").strip()[:80], role=role, salt=salt.hex(),
                              password_hash=_hash(password, salt)))
    except DuplicateKeyError as exc:
        raise AuthError(f"{e} already has an account.") from exc
    store.audit("user_created", user=e, role=role)
    return u


def create_first_admin(email: str, password: str, name: str = "") -> User:
    if not setup_needed():
        raise AuthError("Milo already has users. Sign in, or ask an admin to add you.")
    u = create_user(email, password, "admin", name)
    if store.count(User, {"role": "admin"}) > 1 and store.find(User, sort=[("_id", 1)], limit=1)[0].id != u.id:
        store.db()[User.collection].delete_one({"_id": u.id})  # two browsers raced: the first one wins
        raise AuthError("Milo already has users. Sign in, or ask an admin to add you.")
    return u


def _locked(key: str) -> bool:
    cutoff = time.monotonic() - LOCK_MINUTES * 60
    tries = [t for t in _fails.get(key, []) if t > cutoff]
    _fails[key] = tries
    return len(tries) >= MAX_FAILS


# Private networks (RFC 1918, IPv6 unique-local, link-local): the Docker bridge, a reverse proxy on the LAN.
# Listed on purpose instead of ip.is_private, which also covers documentation and other special ranges.
_PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
                                                "fc00::/7", "fe80::/10")]


def _shared(ip: str) -> bool:
    """An address many users share: loopback (this computer, the ngrok tunnel) or a private network (the Docker
    bridge, a reverse proxy on the LAN). A per-address lock there would lock everyone out."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback or any(a.version == n.version and a in n for n in _PRIVATE)


def login(email: str, password: str, ip: str = "") -> tuple[str, User]:
    """Check the password and start a session. Returns (session id for the cookie, user)."""
    e = (email or "").strip().lower()
    # The per-address lock only means something for a real remote address. On this computer, through a tunnel
    # (127.0.0.1) and under Docker (the bridge address, 172.x) every user shares one address, so it would turn five
    # wrong tries by anyone into a lock-out for everyone; the per-email lock still protects each account there.
    keys = [f"email:{e}"] + ([f"ip:{ip}"] if ip and not _shared(ip) else [])
    if any(_locked(k) for k in keys):
        store.audit("login_locked", user=e)
        raise AuthError(f"Too many wrong tries. Sign-in is locked for {LOCK_MINUTES} minutes.")
    rows = store.find(User, {"email": e}, limit=1)
    u = rows[0] if rows else None
    # always hash, also for an unknown email, so the answer takes the same time either way
    given = _hash(password or "", bytes.fromhex(u.salt) if u else _DUMMY_SALT)
    if not (u and not u.disabled and hmac.compare_digest(given, u.password_hash)):
        for k in keys:
            _fails.setdefault(k, []).append(time.monotonic())
        store.audit("login_failed", user=e)
        raise AuthError("Wrong email or password.")
    _fails.pop(f"email:{e}", None)
    sid = secrets.token_urlsafe(32)
    store.insert(Session(sid_hash=_sid_hash(sid), user_id=u.id,
                         expires_at=now() + timedelta(hours=get_settings().session_hours)))
    store.update(User, u.id, {"last_login_at": now()})
    store.audit("login", user=e)
    return sid, u


def user_for(sid: str) -> User | None:
    """The signed-in user for this cookie value, or None (unknown, expired, signed out or disabled)."""
    if not sid or len(sid) > 200:
        return None
    h = _sid_hash(sid)
    hit = _cache.get(h)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = store.find(Session, {"sid_hash": h}, limit=1)
    s = rows[0] if rows else None
    u = store.get(User, s.user_id) if s and s.expires_at > now() else None
    if u is None or u.disabled:
        _cache.pop(h, None)
        return None
    if len(_cache) > 500:
        _cache.clear()
    _cache[h] = (time.monotonic(), u)
    return u


def logout(sid: str) -> None:
    h = _sid_hash(sid or "")
    _cache.pop(h, None)
    store.db()[Session.collection].delete_many({"sid_hash": h})


def _end_sessions(user_id: int) -> None:
    store.db()[Session.collection].delete_many({"user_id": user_id})
    for h in [h for h, (_, u) in _cache.items() if u.id == user_id]:
        _cache.pop(h, None)


def update_user(user_id: int, *, role: str | None = None, disabled: bool | None = None,
                password: str | None = None, name: str | None = None) -> User:
    u = store.get(User, user_id)
    if u is None:
        raise AuthError("User not found.")
    changes: dict = {}
    if role is not None:
        if role not in ROLES:
            raise AuthError(f"Role must be one of: {', '.join(ROLES)}.")
        changes["role"] = role
    if disabled is not None:
        changes["disabled"] = bool(disabled)
    if name is not None:
        changes["name"] = name.strip()[:80]
    if password is not None:
        _check_password(password)
        salt = secrets.token_bytes(16)
        changes.update(salt=salt.hex(), password_hash=_hash(password, salt))
    losing_admin = u.role == "admin" and not u.disabled and (changes.get("role", "admin") != "admin"
                                                             or changes.get("disabled"))
    if losing_admin and store.count(User, {"role": "admin", "disabled": False}) <= 1:
        raise AuthError("Milo needs at least one active admin. Make someone else an admin first.")
    if not changes:
        return u
    u = store.update(User, user_id, changes)
    _end_sessions(user_id)  # a new role, password or a disabled account takes effect at once
    store.audit("user_changed", user=u.email, changed=sorted(k for k in changes if k not in ("salt", "password_hash"))
                + (["password"] if password is not None else []))
    return u


def forget_login_attempts() -> None:
    """Tests only."""
    _fails.clear()
    _cache.clear()
