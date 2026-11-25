# Deploying Milo

Milo is built to run on **one machine for one business**: the dashboard stays on that machine, and only Meta's
webhooks and media downloads come in from the internet. This guide covers three ways to run it. Everything is
configured with the same `.env` file (copy `.env.example`); no setting is baked into the code or the image.

| Option | When to use it | Command |
| --- | --- | --- |
| A. Directly with `uv` | Development, the demo | `uv run uvicorn app.main:app` |
| B. Docker, database elsewhere | One command on any machine with Docker; MongoDB Atlas | `docker compose up --build` |
| C. Docker with a local MongoDB | No Atlas account, or offline work | `docker compose --profile local-db up --build` |

Then open **http://localhost:8000**. On the first start Milo asks you to create the first admin.

## A. Directly with uv

See *Run it* in the README: `uv sync`, `cp .env.example .env`, `uv run uvicorn app.main:app --reload`.
On Windows, `start-milo.ps1` starts Milo and the ngrok tunnel together.

## B. Docker (MongoDB Atlas or any MongoDB you already have)

1. Install Docker Desktop (Windows / macOS) or Docker Engine (Linux).
2. `cp .env.example .env` and fill it in. `MONGODB_URI` points at Atlas as usual.
3. `docker compose up --build`

What the files do:
- `Dockerfile`: Python 3.12 slim image, dependencies installed from `uv.lock` (exact versions, no dev tools),
  runs as a non-root user, one uvicorn worker, health check on `/api/health`.
- `docker-compose.yml`: publishes the dashboard on **127.0.0.1:8000 only** (not on your network), reads `.env`,
  and keeps uploaded and generated images in the `milo-data` volume so they survive rebuilds.
- `.dockerignore`: `.env`, `.git`, `.venv`, `data/` and tests never go into the image.

Useful commands: `docker compose logs -f milo` (server log), `docker compose down` (stop),
`docker compose exec milo python scripts/add_user.py --list` (manage users inside the container).

## C. Docker with a local MongoDB

Set `MONGODB_URI=mongodb://mongo:27017` in `.env`, then run `docker compose --profile local-db up --build`.
The database lives in the `mongo-data` volume. It is reachable only by Milo, not from your network.

## Meta webhooks (DMs, comments) and Instagram media

Meta has to reach two paths over public HTTPS: `/webhooks/meta` (signed with `META_APP_SECRET`) and
`/media/<token>/<file>` (post media, random token). Use a tunnel on the same machine, exactly as without Docker:

```bash
ngrok http --domain=<your-domain> 127.0.0.1:8000        # or: cloudflared tunnel --url http://localhost:8000
```

Put that address in `PUBLIC_BASE_URL` and in the Meta App Dashboard (README, *DM webhook*). Everything else
(the dashboard and `/api`) refuses requests that come through the tunnel unless `API_TOKEN` is set.

## Checklist before running it for real

| Setting | Why |
| --- | --- |
| `AUTH_REQUIRED=true` (default) | Everyone signs in; only admins approve. |
| `META_DRY_RUN=false` only when ready | `true` never calls Meta. |
| `META_ALLOW_SPEND=false` unless the account owner agreed | `false` keeps every ad paused, so nothing is charged. Turn it back off after a demo. |
| `DM_AUTOREPLY=false` until the inbox has been watched for a while | Drafts only; a person presses Send. |
| `MAX_DAILY_BUDGET_INR`, `MAX_TOTAL_DAILY_BUDGET_INR` | Hard caps checked at request and at execution. |
| `META_APP_ID` = the app that owns the token | Lets Milo tell its own echoes from a person's replies. |
| MongoDB Atlas *Network Access* includes this machine's IP | Otherwise the connection fails with a TLS error (`TLSV1_ALERT_INTERNAL_ERROR`). |
| Back up the database | Atlas: enable backups. Local: `docker compose exec mongo mongodump --out /data/db/backup`. |
| One worker only | Milo keeps per-process locks and caches; don't add `--workers` or replicas. |

## Running Milo beyond localhost (not the default)

Milo is designed for one machine. If the dashboard must be reachable from elsewhere, put it behind an HTTPS
reverse proxy, set `ALLOWED_HOSTS` to that host name, set a long random `API_TOKEN`, and keep `AUTH_REQUIRED=true`.
Any container host that runs a Docker image and gives HTTPS can run it this way, with one instance and MongoDB
Atlas as the database.
