# Milo in a container. Same app as `uv run uvicorn app.main:app`; settings come from .env (never baked in).
#   docker compose up --build        -> http://localhost:8000   (see DEPLOY.md)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir "uv>=0.5"

WORKDIR /app
# dependencies first (cached while only the code changes); exact versions from uv.lock, no dev tools
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY web ./web
COPY scripts ./scripts

# run as a normal user; data/ holds uploaded and generated images (mount a volume to keep them)
RUN useradd --create-home --uid 10001 milo && mkdir -p /app/data && chown -R milo:milo /app/data
USER milo

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1

# one worker on purpose: Milo keeps per-process locks and caches (see README, Security)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
