# Milo — Tech Stack

Milo is a conversational AI agent that creates, approves, publishes and monitors **Meta (Facebook + Instagram)** ad
campaigns on a **live Meta business ad account**. The software stack is free: free LLM models, MongoDB Atlas free tier,
and spending on Meta is **off by default** (campaigns are created paused until the account owner enables spend).

## At a glance

| Layer | Technology | Version | Why we chose it |
| --- | --- | --- | --- |
| Language | Python | 3.12+ | One language across agent, API and jobs; the Meta Ads CLI also needs 3.12+ |
| Agent framework | Google ADK (`google-adk`) | ≥ 1.0 (tested on 2.11) | `McpToolset` loads MCP tools; `before_tool_callback` = tool guard; built-in sessions and runner |
| Tool protocol | Model Context Protocol — `mcp` Python SDK (FastMCP) | 1.x (tested on 1.30) | Meta tools live in a separate `milo-meta` MCP server over stdio; reusable from Claude Desktop / VS Code |
| LLM gateway | LiteLLM | ≥ 1.50 | Lets ADK talk to any provider; changing models is one `.env` line |
| LLM provider | OpenRouter free models | `openrouter/openrouter/free` (default) | No cost; can pin a specific tool-capable `:free` model via `MILO_MODEL` |
| Backend API | FastAPI + Pydantic | ≥ 0.115 | Async, typed, auto-docs at `/docs`; serves the dashboard too |
| Server | Uvicorn | ≥ 0.30 | ASGI server for FastAPI |
| Chat streaming | Server-Sent Events (`sse-starlette`) + ADK `StreamingMode.SSE` | ≥ 2.1 | Replies appear as they are written; tool steps and approval cards stream too; model reasoning is filtered out |
| Ads platform | Meta Marketing API (Graph API) | **v26.0** | One API for Facebook and Instagram ads; business system-user tokens; best documented ads API |
| HTTP client | httpx | ≥ 0.27 | Async; lets us read Meta's rate-limit headers directly; mockable with respx |
| Database | MongoDB (Atlas free M0) + PyMongo | PyMongo ≥ 4.8 | Shared by the team in the cloud; documents fit campaigns/approvals naturally; atomic `find_one_and_update` for the approval claim; tests use `mongomock` |
| Background jobs | APScheduler (AsyncIOScheduler) | 3.x | In-process 15-minute metrics pull + optimizer run; no Redis/Celery needed |
| Optimization | Rule engine (`app/optimizer.py`) | — | Deterministic, explainable recommendations filed as approvals; no LLM cost |
| Ad image generation | **FLUX.1 [schnell]** (Black Forest Labs) on **Cloudflare Workers AI**, model id `@cf/black-forest-labs/flux-1-schnell` | REST, 4 steps, 1024×1024 | Free daily allowance (10,000 neurons ≈ 150+ images), fast, Apache-2.0 licence allows ad use; called with httpx and mocked with respx in tests |
| Ad research | Tavily Search + Extract API (via httpx) | REST | Recent web context for ad copy (`time_range`, published dates), built for LLM agents; free 1,000 credits/month; optional |
| Config & secrets | pydantic-settings + `.env` | ≥ 2.4 | Typed settings; secrets never reach the browser |
| Frontend | Single-file HTML + vanilla JS, Tailwind CSS (prebuilt `web/milo.css`), hand-drawn SVG charts | Tailwind 3 (build only) | Served by FastAPI at `/` — same origin as the API, so no CORS, no hosting, no Node at runtime. Design: warm paper palette, Fraunces + Outfit |
| Testing | pytest + pytest-asyncio + respx + mongomock | — | 85 tests: MCP tools, approval gate (incl. CSRF, double approve, failures), HTTP API, chat streaming, optimizer, Meta, Tavily and Cloudflare image clients, without calling the LLM, Meta, Tavily, Cloudflare or a real database |
| Packaging | uv | — | Fast installs and a lockfile (`uv.lock`) for reproducible setup |

## Key decisions

### 1. Meta as the ad platform
Meta was the only platform that gives a **free sandbox ad account** (no payment method, ads never run) with
**self-serve API access** to accounts you own. Pinterest and TikTok were runners-up; LinkedIn, Reddit and X need
approval or have no test environment. Full comparison: *Milo — Platform Research* doc.
Development started on that sandbox; the project now runs on the team's live Meta business account, which also
covers Instagram through the same API.

### 2. Google ADK as the agent harness
- `LlmAgent` = the agent loop; its tools come from the MCP server through `McpToolset`.
- `before_tool_callback` = the **tool guard**: allow-list of tool names + audit log of every change request.
- `Runner` + `InMemorySessionService` = per-chat conversation state.
- Same framework as our earlier content-pipeline and MCP capstone projects.

### 2b. MCP for the tool layer
- Tools (`create_campaign_draft`, `list_campaigns`, `get_campaign_metrics`, `request_publish`, `request_pause`,
  `request_budget_change`, `list_pending_approvals`, `research_topic`, `read_landing_page`) live in the **milo-meta MCP server** (`app/mcp_server/server.py`, FastMCP, stdio).
- **Request-only by design:** the MCP server can draft, read and *request* changes, but no MCP tool can change a
  live ad. `request_*` tools store a pending Approval; only the dashboard's Approve button runs `actions.py`.
  This holds even if another MCP client (Claude Desktop) calls the tools.
- Agent and platform are decoupled: adding another ad platform = another MCP server, no agent changes.
- Pinned to `mcp<2`: the 2.x SDK renamed FastMCP and changed APIs.

### 2c. Why not n8n
n8n would move the orchestration we are evaluated on (agent harness, approvals, optimisation) into visual
workflows and add another service to run. It is listed as optional future work for notifications only.

### 3. OpenRouter free models via LiteLLM
- Cost: ₹0. Limits: **20 requests/min, 50 requests/day** (1,000/day after a one-time $10 credit purchase).
- A chat turn with tools uses ~2–4 LLM requests, so Milo keeps the LLM out of everything that code can do:
  validation, approvals, Meta calls and metrics all run in plain Python.
- Swap models with `MILO_MODEL` in `.env` — no code change.

### 4. Direct Graph API calls (httpx) instead of the official `facebook-business` SDK
- We need Meta's rate-limit headers (`X-Business-Use-Case-Usage`, `X-Ad-Account-Usage`,
  `X-FB-Ads-Insights-Throttle`) on every response to slow down before we get blocked.
- httpx is mockable with respx, so the Meta client is fully tested offline. The SDK uses `requests`, which respx
  can't mock.
- `META_DRY_RUN=true` returns fake IDs, so development never touches Meta.

### 5. Live account with spend switched off by default
- The team moved from a sandbox to a **live business ad account** (Facebook + Instagram). To avoid spending company
  money by accident, `META_ALLOW_SPEND=false` is the default: Approve creates the campaign, ad set, creative and ad
  on Meta but keeps them **PAUSED**, so nothing delivers and nothing is charged. Resuming is refused until the owner
  sets `META_ALLOW_SPEND=true`.
- Hard caps on every budget (`MAX_DAILY_BUDGET_INR` per campaign, `MAX_TOTAL_DAILY_BUDGET_INR` overall) are checked
  when a change is requested and again when it is executed, for Milo, the UI buttons and the optimizer alike.
- Metrics come from real Insights (`METRICS_PROVIDER=meta`). Dry run uses `SimulatedInsightsProvider`, which only
  counts running campaigns from their creation date, and every optimizer reason based on it says so.

### 5b. Facebook + Instagram in one ad set
- One ad set with `publisher_platforms` = the chosen apps; Meta picks the positions (Feed, Stories, Reels).
- The creative carries the Page and the linked Instagram account (`instagram_user_id`, the v22+ replacement for
  `instagram_actor_id`), found from the Page's `instagram_business_account` or set in `.env`.
- Instagram needs a picture, so campaigns have an ad image (uploaded in the UI, sent to `/adimages`, used by hash).

### 6. Frontend served by the backend (and why not Apper or a React app)
- `web/index.html` is one file of HTML + vanilla JS, served by FastAPI at `/`. Same origin as the API, so no CORS,
  no hosting, no login system and no browser "local network" prompts; evaluators run one command and open
  `http://localhost:8000`.
- Design follows the Aether AI reference: warm paper palette, Fraunces + Outfit, 3-column dashboard with the chat in
  the centre. Every number comes from the API; the reference's hardcoded word cloud and sample activity were dropped.
- Tailwind is **prebuilt** into `web/milo.css` (design tokens in `web/tailwind.config.js`), so the page needs no CDN
  and no Node at runtime. Charts are hand-drawn SVG, with no chart library.
- Tried and dropped: an **Apper**-hosted React frontend (separate domain → CORS and Chrome local-network prompts; a
  chart-library chunk that never loaded kept the app on its boot screen) and the **Grok-generated TanStack app**
  (fake Meta publishing, paid xAI model, data only in browser storage, a second approval system).

### 7. Rule-based optimizer that recommends, never applies
- `app/optimizer.py` runs after each metrics pull: pause campaigns whose CTR is far below the account average,
  raise budget on the best performer, trim budget where cost per click is high.
- Rules instead of the LLM: deterministic, explainable, free (no OpenRouter requests), and testable. Every
  recommendation carries its numbers as a plain-language reason, and Milo explains it in chat when asked.
- Output is a pending approval (`source = optimizer`), so the approval gate applies unchanged. Cooldowns keep it
  inside Meta's limit of 4 budget changes per ad set per hour and stop it nagging about rejected ideas.
- Simulated metrics give each campaign a stable "quality" so campaigns differ and the loop has something to find.

### 8. Tavily for ad research ("recent context", not trends)
- Before writing copy, Milo calls the `research_topic` MCP tool once (business + city). `app/research/tavily.py`
  sends one basic Tavily search limited to the past month (`time_range`) with published dates, keeps only http(s)
  results and trims each snippet to 350 characters. `read_landing_page` uses Tavily Extract (https only, 2,500
  characters) so the copy matches the real offer.
- Why Tavily: a search API made for LLM agents, returns clean snippets and dates in one call, has a recency filter,
  and has a free plan (1,000 credits/month; a basic search is 1 credit). Called with httpx, so tests mock it with
  respx like the Meta client.
- Honest framing: search results show what is being written about recently. They are not trend data (no search
  volume), so Milo calls them "recent context" and cites the source and date behind every headline option.
- Safety: web pages are untrusted. Results are labelled as information only, the prompt tells Milo never to follow
  instructions in them, and the tool guard + approval gate mean web text cannot cause any live change.
- Optional: with no key the tool returns "unavailable" and Milo still writes copy, saying it was not researched.
- Rejected: **Google Trends API** (alpha, invite-only), **pytrends** (unofficial, archived April 2025),
  **Meta Ad Library API** (outside the EU it only returns political/issue ads, not ordinary commercial ads).

### 8b. FLUX.1 [schnell] on Cloudflare Workers AI for ad images
- Instagram ads need a picture, so Milo can generate one: `app/creative/imagegen.py` calls
  `@cf/black-forest-labs/flux-1-schnell` (FLUX.1 [schnell], Black Forest Labs, Apache-2.0) through the Workers AI
  REST API and saves the JPEG as the draft's ad image. The agent tool is `generate_ad_image`; the UI has
  **✨ Generate image**.
- Why this model: it is the free option that works today. Workers AI includes 10,000 neurons per day and one 4-step
  1024×1024 image costs about 58, so roughly 150+ images a day; `IMAGE_DAILY_LIMIT` (default 40) keeps Milo well inside.
  Google's Gemini image models ("Nano Banana") have no free API tier as of October 2026 (about $0.03–0.13 per
  image); Hugging Face's free inference is heavily rate-limited; OpenRouter image models are paid.
- Every prompt asks for a photo with **no text, letters or logos**: image models misspell words, and Meta shows the
  ad's text, headline and button anyway.
- The approval gate is unchanged: a generated image only changes the draft, and reaches Meta when publishing is approved.
- The model is a setting (`IMAGE_MODEL`); newer Workers AI image models (for example FLUX.2) can be tried there,
  but they use different pricing units, so check the free allowance first.

### 9. MongoDB Atlas + APScheduler instead of a SQL database + Celery
- MongoDB Atlas (free M0) lets the team share one database in the cloud with no server to run. The web server and
  the MCP server (separate processes) use the same database.
- A small data layer (`app/db/store.py`) keeps readable sequential ids (`counters` collection) and provides an
  atomic `claim()` (find-one-and-update), which makes Approve safe against double clicks.
- APScheduler runs in-process: no Redis or Celery needed.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | — | OpenRouter key (required for chat) |
| `MILO_MODEL` | `openrouter/openrouter/free` | LiteLLM model string |
| `META_API_VERSION` | `v26.0` | Pinned Graph API version |
| `META_ACCESS_TOKEN` | — | Business system-user token: `ads_management`, `ads_read`, `pages_read_engagement`, `pages_show_list`, `instagram_basic`, `business_management` |
| `META_AD_ACCOUNT_ID` | — | Ad account id (digits; an `act_` prefix is also accepted) |
| `META_PAGE_ID` | — | Facebook Page the ads run under |
| `META_INSTAGRAM_ACCOUNT_ID` | auto | Instagram account linked to the Page (found from the Page if empty) |
| `META_PAGE_POST_ID` | — | Optional: boost an existing Page post instead of Milo's own ad copy |
| `META_DRY_RUN` | `true` | `true` = never call Meta |
| `META_ALLOW_SPEND` | `false` | `false` = campaigns are created on Meta but stay paused (no charge); resume refused |
| `MAX_DAILY_BUDGET_INR` | `500` | Hard cap per campaign, every request |
| `MAX_TOTAL_DAILY_BUDGET_INR` | `1000` | Hard cap across running campaigns |
| `METRICS_PROVIDER` | `meta` | `meta` or `simulated` (dry run always uses simulated) |
| `METRICS_POLL_MINUTES` | `15` | Metrics pull interval |
| `MONGODB_URI` | `mongodb://localhost:27017` | MongoDB connection string (Atlas `mongodb+srv://…`) |
| `MONGODB_DB` | `milo` | Database name |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host names the server answers to (blocks DNS rebinding) |
| `API_TOKEN` | — | If set, every `/api` request needs `Authorization: Bearer <token>` |
| `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare account for Workers AI (ad images) |
| `CLOUDFLARE_API_TOKEN` | — | Workers AI token (Read + Edit) |
| `IMAGE_MODEL` | `@cf/black-forest-labs/flux-1-schnell` | Image model: FLUX.1 [schnell] on Cloudflare Workers AI |
| `IMAGE_STEPS` | `4` | Diffusion steps (1–8) |
| `IMAGE_DAILY_LIMIT` | `40` | Milo's own cap on generated images per day |
| `MILO_ENV_FILE` | `.env` | Which env file to read; tests set it to empty so a personal `.env` never affects them |
| `OPTIMIZER_ENABLED` | `true` | Run the optimizer after each metrics pull |
| `OPTIMIZER_*` thresholds | see `.env.example` | Min impressions, pause / scale / trim ratios, budget step, max budget, cooldown |
| `TAVILY_API_KEY` | — | Tavily key for ad research (optional; without it copy is written unresearched) |
| `RESEARCH_ENABLED` | `true` | Turn web research on/off |
| `RESEARCH_TIME_RANGE` | `month` | How recent results must be: `day`, `week`, `month`, `year` |
| `RESEARCH_MAX_RESULTS` | `5` | Sources per search |
| `CORS_ORIGIN_REGEX` | `http://(localhost\|127.0.0.1)(:port)?` | Extra browser origins allowed to call the API (only for a separate local dev server) |

## Platform limits we design around

| Limit | Value | How Milo handles it |
| --- | --- | --- |
| OpenRouter free requests | 20/min, 50/day | Code does validation, approvals, metrics; LLM only for conversation. Pin a fast non-reasoning `:free` model for quicker replies |
| Meta `ads_management` (standard access) | 300 + 40 × active ads per hour per ad account | Throttled client, slows down above 75% usage, backoff on codes 4/17/613/80000–80014 |
| Meta `ads_insights` (standard access) | 600 + 400 × active ads per hour | Metrics stored in MongoDB, pulled every 15 min, all pages followed |
| Meta ad set budget changes | 4 per hour per ad set | Budget changes go through approval; the optimizer recommends at most one change per campaign per 6 h |
| Live ad account spend | Real money once ads deliver | `META_ALLOW_SPEND=false` by default (everything paused) + per-campaign and total daily caps |
| Cloudflare Workers AI free plan | 10,000 neurons/day (FLUX.1 [schnell] ≈ 58 per image) | `IMAGE_DAILY_LIMIT` (default 40/day); clear message when the allowance is used up; upload always works |
| Tavily free plan | 1,000 credits/month | One basic search (1 credit) per campaign idea; research is optional and each call is logged |
