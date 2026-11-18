"""Mask anything that looks like a secret before error text reaches the browser or the logs shown in the UI."""
import re

_PATTERNS = [
    re.compile(r"EAA[A-Za-z0-9]{20,}"),               # Meta access tokens
    re.compile(r"sk-or-[A-Za-z0-9-]{10,}"),           # OpenRouter keys
    re.compile(r"tvly-[A-Za-z0-9-]{10,}"),            # Tavily keys
    re.compile(r"\b\d{6,}\|[0-9a-f]{32}\b"),            # Meta app access token (app id | app secret)
    re.compile(r"(access_token|api_key|token|secret)=([^&\s\"']+)", re.I),
    re.compile(r"mongodb(\+srv)?://[^@\s]+@", re.I),  # credentials in a MongoDB URI
    re.compile(r"(?<=Bearer )[A-Za-z0-9._~+/=-]{12,}"),  # any Authorization header value (Cloudflare, OpenRouter)
]

_SECRET_FIELDS = ("openrouter_api_key", "tavily_api_key", "meta_access_token", "meta_page_access_token",
                  "meta_app_secret", "meta_webhook_verify_token", "cloudflare_api_token", "api_token")


def _configured_secrets() -> list[str]:
    try:
        from app.config import get_settings

        s = get_settings()
        return [v for v in (getattr(s, f, "") for f in _SECRET_FIELDS) if isinstance(v, str) and len(v) >= 8]
    except Exception:  # noqa: BLE001 - redaction must never fail (it runs inside error handlers)
        return []


def redact(text: object) -> str:
    out = str(text)
    for secret in _configured_secrets():  # exact values from .env, whatever they look like (e.g. Cloudflare tokens)
        out = out.replace(secret, "***")
    for p in _PATTERNS:
        out = p.sub(lambda m: (m.group(1) + "=***") if m.lastindex and m.group(1) and "=" in m.group(0)
                    else ("mongodb://***@" if m.group(0).lower().startswith("mongodb") else "***"), out)
    return out
