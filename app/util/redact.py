"""Mask anything that looks like a secret before error text reaches the browser or the logs shown in the UI."""
import re

_PATTERNS = [
    re.compile(r"EAA[A-Za-z0-9]{20,}"),               # Meta access tokens
    re.compile(r"sk-or-[A-Za-z0-9-]{10,}"),           # OpenRouter keys
    re.compile(r"tvly-[A-Za-z0-9-]{10,}"),            # Tavily keys
    re.compile(r"(access_token|api_key|token|secret)=([^&\s\"']+)", re.I),
    re.compile(r"mongodb(\+srv)?://[^@\s]+@", re.I),  # credentials in a MongoDB URI
]


def redact(text: object) -> str:
    out = str(text)
    for p in _PATTERNS:
        out = p.sub(lambda m: (m.group(1) + "=***") if m.lastindex and m.group(1) and "=" in m.group(0)
                    else ("mongodb://***@" if m.group(0).lower().startswith("mongodb") else "***"), out)
    return out
