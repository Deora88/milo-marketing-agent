# Milo — Meta ads agent

Milo is a conversational AI agent that **creates, approves, publishes and monitors** Meta (Facebook + Instagram)
ad campaigns. Every live change (publish, pause, budget edit) stops at a human approval gate.

## Stack
Python 3.12 · Google ADK · MCP (milo-meta server) · LiteLLM → OpenRouter (free models) · FastAPI + SSE · httpx → Meta Marketing API v26.0 ·
SQLite + SQLModel · APScheduler · Tavily web search (ad research) · HTML/JS dashboard (Tailwind, prebuilt CSS)

## Frontend
The UI lives in `web/` and is served by the backend itself: run the server and open **http://localhost:8000**.
Design: warm "paper" palette, Fraunces + Outfit fonts, 3-column dashboard (agent status · spend chart + chat · performance and approvals).
Screens: Dashboard (with chat) · Campaigns (+ detail) · Approvals · Analytics · Activity · Settings.
`web/index.html` is plain HTML/JS — no Node needed. Styles are prebuilt into `web/milo.css`; rebuild only if you change classes:
`npx tailwindcss@3 -c web/tailwind.config.js -i web/tailwind.input.css -o web/milo.css --minify`

## Run it
```bash
uv sync                      # installs dependencies into .venv
cp .env.example .env         # add OPENROUTER_API_KEY (+ optional TAVILY_API_KEY; Meta values can wait: META_DRY_RUN=true)
uv run uvicorn app.main:app --reload
# open http://localhost:8000
uv run pytest                # tests never call the LLM or Meta
```
No `uv`? `pip install uv` first (Windows: `python -m pip install uv`).

**Slow replies?** The default `openrouter/openrouter/free` router picks a random free model, often a slow "thinking"
one. Pin one fast free model that supports tools in `.env` (`MILO_MODEL=openrouter/<provider>/<model>:free`).
The server log prints `chat turn: first response Xs, total Ys` for every reply.

## How a request flows
chat UI → `POST /api/chat` (SSE) → ADK agent `milo` → tool guard (`before_tool_callback`) →
**milo-meta MCP server** (stdio) → `request_*` tool stores a pending Approval → user clicks Approve →
`app/actions.py` calls Meta → SQLite → dashboard.

Before writing ad copy Milo calls `research_topic` (Tavily web search, past month by default) and, if you give a URL,
`read_landing_page`. It offers 2–3 headline options, each citing the sources and dates it was based on. See
[Ad research](#ad-research-web-search).

Every metrics pull also runs the **optimizer**: rule checks on the last 7 days (pause weak campaigns, raise budget on
the best, trim expensive ones). Each idea becomes an approval card with a plain-language reason; nothing is applied
without your click.

Full diagrams: [ARCHITECTURE.md](ARCHITECTURE.md) · stack and decisions: [TECH_STACK.md](TECH_STACK.md)

## Project layout
```
app/
  main.py            FastAPI app; serves web/ and the API
  config.py          settings from .env
  actions.py         live actions, executed only after approval
  approvals.py       creates pending approval requests (status rules, no duplicates)
  optimizer.py       rule-based optimization loop -> recommendations filed as approvals
  research/          Tavily client: recent web search + landing page extract (trimmed, untrusted)
  mcp_server/        milo-meta MCP server: draft, list, metrics, request_*, research tools
  agent/             ADK agent (McpToolset), prompt, tool guard, runner
  meta/              throttled Meta client (usage headers, backoff, dry-run) + campaign builder
  metrics/           MetricsProvider interface: simulated (sandbox) and Meta Insights
  jobs/              APScheduler: pulls metrics every 15 min
  db/                SQLModel tables: Campaign, Approval, MetricSnapshot, AuditLog
  api/               routes: chat (SSE), campaigns (+ change requests), approvals, insights (status, summary, audit)
web/
  index.html         the Milo UI (single file, served at /)
  milo.css           prebuilt Tailwind styles; tokens in tailwind.config.js
tests/               MCP tools + approvals, MCP over stdio, HTTP API, chat streaming, Meta client, optimizer, research (respx mocks)
```

## Modes
| Setting | Development | Real sandbox | Real metrics |
| --- | --- | --- | --- |
| `META_DRY_RUN` | `true` (no Meta calls) | `false` | `false` |
| `METRICS_PROVIDER` | `simulated` | `simulated` (sandbox has no Insights) | `meta` |

**Meta app in Development mode?** Meta blocks ad posts created by a Development-mode app (error 1885183). Either
switch the app to Live, or make a post on your Page and set `META_PAGE_POST_ID` (post id or URL) in `.env`: Milo
then uses that post as the ad. Publishing resumes where it stopped if a step fails, so no duplicate campaigns.

## Optimization loop
Runs after every metrics pull (every `METRICS_POLL_MINUTES`) and from **Run optimizer** in the UI. Rules, on live
campaigns with at least 1,000 impressions in 7 days, compared with the account average:

| Signal | Recommendation |
| --- | --- |
| CTR below 60% of average (or below 0.6% when only one campaign is live) | Pause |
| CTR at least 120% of average and cost per click at or below average | Raise daily budget 20% (max ₹5,000) |
| Cost per click above 130% of average | Trim daily budget 20% (min ₹100) |

One recommendation per campaign every 6 hours, never while another request is pending, and a rejected idea is not
repeated for 24 hours. All thresholds are `OPTIMIZER_*` settings in `.env`. Milo can explain any recommendation in chat.

## Ad research (web search)
When you describe what to promote ("a bakery in Pune"), Milo calls `research_topic` once with the business and
city. The tool asks Tavily for pages from the past `RESEARCH_TIME_RANGE` (default `month`) and returns up to
`RESEARCH_MAX_RESULTS` sources: title, URL, published date and a short snippet. Milo then writes 2–3 headline/text
options, each with a "based on" note. The chat shows the sources under the reply.

- **"Recent context", not "trends":** search results show what is being written about now. They are not search
  volume or trend data, and Milo is told never to call them trends.
- **Untrusted:** web text is labelled as information only. The tool guard and approval gate still apply, so a page
  that says "publish now" cannot change anything. Snippets are trimmed (350 chars, landing pages 2,500).
- **Optional:** without `TAVILY_API_KEY` (or with `RESEARCH_ENABLED=false`) the tool returns "unavailable" and Milo
  writes the copy anyway, saying it was not researched.
- **Cost:** Tavily's free plan gives 1,000 credits/month; one basic search = 1 credit, one page extract ≈ 1 credit.
  Each research call is logged in Activity.
- Not used: Google Trends API (alpha, invite only), `pytrends` (archived April 2025), Meta Ad Library API
  (does not return ordinary commercial ads outside the EU).

## Free-tier limits to remember
- OpenRouter `:free` models: 20 requests/min, 50 requests/day (1,000/day after a one-time $10 credit purchase).
  One chat turn with tools is about 2–4 requests. Validation, approvals and metrics run in code, not via the LLM.
- Tavily free plan: 1,000 API credits per month (about 1 per research call).
- Meta dev tier: about 300 management + 600 Insights calls per hour per ad account; 4 budget changes per ad set per hour.

## API
| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | The Milo UI (`web/index.html`) |
| POST | `/api/chat` | Chat turn, streamed as SSE events: `delta`, `message`, `tool_call`, `tool_result`, `approval`, `error`, `done` |
| GET | `/api/campaigns?status=&q=` | Campaigns, optionally filtered by status / name |
| GET | `/api/campaigns/{id}` | One campaign |
| GET | `/api/campaigns/{id}/metrics` | Stored daily metrics |
| GET | `/api/approvals?status=pending` | Approvals (`pending`, `approved`, `rejected`, `all`), with campaign name |
| POST | `/api/approvals/{id}/approve` · `/reject` | Human decision; approve executes the action |
| POST | `/api/metrics/refresh` | Pull metrics now (also runs the optimizer) |
| POST | `/api/optimizer/run` | Run the optimizer now; returns recommendations filed and ideas skipped (cooldown / already pending) |
| POST | `/api/campaigns/{id}/requests` | UI buttons: request publish / pause / budget change (creates an approval only) |
| GET | `/api/campaigns/{id}/approvals` | Approval history for one campaign |
| GET | `/api/campaigns/{id}/preview?format=` | Read-only: Meta's rendering of the ad (Facebook mobile/desktop feed, Instagram feed/story), shown on the campaign page |
| GET | `/api/status` | Model, dry-run, Meta, metrics, optimizer and research settings (no secrets) |
| GET | `/api/summary` | Dashboard KPIs and daily spend |
| GET | `/api/audit` | Activity log |

## MCP server (milo-meta)
Milo's tools live in an MCP server, so any MCP client can use them. The ADK agent starts it automatically.

| Tool | What it does |
| --- | --- |
| `create_campaign_draft` | Validates and saves a draft locally; countries, or a local area (place + latitude/longitude + 1–80 km radius) for shops and restaurants |
| `list_campaigns` | Lists campaigns |
| `get_campaign_metrics` | Daily spend, impressions, clicks, CTR, CPC |
| `request_publish` / `request_pause` / `request_budget_change` | Creates a pending approval only — never changes a live ad |
| `list_pending_approvals` | What is waiting for the user, with the optimizer's reason for each recommendation |
| `research_topic` | Recent web context for a business/topic and city (Tavily, time-limited); results marked untrusted |
| `read_landing_page` | Trimmed text of an https landing page, so copy matches the real offer |

Run it on its own: `uv run python -m app.mcp_server.server` (stdio).

**Use from Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "milo-meta": {
      "command": "uv",
      "args": ["--directory", "D:\\Projects\\Milo marketing agent", "run", "python", "-m", "app.mcp_server.server"]
    }
  }
}
```
Approvals requested from Claude Desktop still appear in the Milo dashboard and need your click.

## Future work
- **n8n notifications (optional):** call an n8n webhook to send Telegram/email alerts when an approval is waiting
  or a campaign's CPC spikes. Kept out of the core so the agent harness stays in code.
