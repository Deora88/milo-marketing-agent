# Milo — Meta ads agent

Milo is a conversational AI agent that **creates, approves, publishes and monitors** Meta (Facebook + Instagram)
ad campaigns on a live Meta business ad account. Every live change (publish, pause, budget edit) stops at a human
approval gate, and **spending is off by default**: campaigns are created on Meta but stay paused until the account
owner turns spending on.

## Stack
Python 3.12 · Google ADK · MCP (milo-meta server) · LiteLLM → OpenRouter (free models) · FastAPI + SSE · httpx → Meta Marketing API v26.0 ·
MongoDB (Atlas) via PyMongo · APScheduler · Tavily web search (ad research) · **FLUX.1 [schnell]** image model on
Cloudflare Workers AI (`@cf/black-forest-labs/flux-1-schnell`, ad images) · HTML/JS dashboard (Tailwind, prebuilt CSS)

## Frontend
The UI lives in `web/` and is served by the backend itself: run the server and open **http://localhost:8000**.
Design: warm "paper" palette, Fraunces + Outfit fonts, 3-column dashboard (agent status · spend chart + chat · performance and approvals).
Screens: Dashboard (with chat) · Campaigns (+ detail) · Approvals · Analytics · Activity · Settings.
`web/index.html` is plain HTML/JS — no Node needed. Styles are prebuilt into `web/milo.css`; rebuild only if you change classes:
`npx tailwindcss@3 -c web/tailwind.config.js -i web/tailwind.input.css -o web/milo.css --minify`

## Run it
```bash
uv sync                      # installs dependencies into .venv
cp .env.example .env         # MONGODB_URI + OPENROUTER_API_KEY (+ optional TAVILY_API_KEY); Meta values can wait
                             # while META_DRY_RUN=true
uv run uvicorn app.main:app --reload
# open http://localhost:8000
uv run pytest                # 110 tests, in-memory MongoDB; ignores your .env; never calls the LLM, Meta or Tavily
```
No `uv`? `pip install uv` first (Windows: `python -m pip install uv`).

**MongoDB Atlas (free):** create an M0 cluster, add a database user, allow your IP under *Network Access*, then copy
the connection string into `MONGODB_URI`. Settings shows "Database (MongoDB): Connected" when it works.

**Slow replies?** The default `openrouter/openrouter/free` router picks a random free model, often a slow "thinking"
one. Pin one fast free model that supports tools in `.env` (`MILO_MODEL=openrouter/<provider>/<model>:free`).
The server log prints `chat turn: first response Xs, total Ys` for every reply.

## How a request flows
chat UI → `POST /api/chat` (SSE) → ADK agent `milo` → tool guard (`before_tool_callback`) →
**milo-meta MCP server** (stdio) → `request_*` tool stores a pending Approval → user clicks Approve →
`app/actions.py` calls Meta → MongoDB → dashboard.

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
  creative/          ad image generation: FLUX.1 [schnell] on Cloudflare Workers AI (no text in images, daily cap)
  mcp_server/        milo-meta MCP server: draft, list, metrics, request_*, research tools
  agent/             ADK agent (McpToolset), prompt, tool guard, runner
  meta/              throttled Meta client (usage headers, backoff, dry-run) + campaign builder
  metrics/           MetricsProvider interface: Meta Insights (live) and simulated (dry run / demo)
  jobs/              APScheduler: pulls metrics every 15 min (per-campaign failures don't stop the pull)
  db/                MongoDB: models (Campaign, Approval, MetricSnapshot, AuditLog, InboundMessage, FaqEntry, Lead) + store (ids, atomic claim)
  dm/inbox.py        DM conversations, Meta's reply window, replies through the Send API
  dm/agent.py        DM bot: FAQ answers, lead capture, hand-off (drafts unless DM_AUTOREPLY=true)
  security.py        Host allow-list, X-Milo-Client header on changes (CSRF), optional API token
  util/redact.py     masks tokens and keys in error text
  api/               routes: chat (SSE), campaigns (+ change requests), approvals, insights (status, summary, audit), webhooks (Meta DMs), inbox (DM threads + replies), faq (bot FAQ + leads)
web/
  index.html         the Milo UI (single file, served at /)
  milo.css           prebuilt Tailwind styles; tokens in tailwind.config.js
tests/               110 tests: MCP tools + approval gate (caps, spend switch, CSRF, double approve, failures), MCP over
                     stdio, HTTP API, chat streaming, Meta client + campaign builder, previews, optimizer, research, image generation,
                     DM webhook + inbox + bot (in-memory MongoDB via mongomock, HTTP mocked with respx; a personal .env is ignored)
```

## Modes and spend safety
| Setting | Dry run (default) | Live, no spend | Live, spending |
| --- | --- | --- | --- |
| `META_DRY_RUN` | `true` (no Meta calls) | `false` | `false` |
| `META_ALLOW_SPEND` | — | `false` (default) | `true` (owner's decision) |
| What Approve does | logged only, fake ids | creates campaign + ad set + creative + ad on Meta, **all paused** | creates and activates |
| Metrics | simulated (labelled) | Meta Insights (no delivery = no new numbers) | Meta Insights |

- Hard caps apply to every budget, whoever asks (Milo, the UI buttons, the optimizer): `MAX_DAILY_BUDGET_INR` per
  campaign (default ₹500) and `MAX_TOTAL_DAILY_BUDGET_INR` across running campaigns (default ₹1,000).
- With spending off, a *resume* request is refused with a clear message; *pause* and budget edits still work.
- Ads run on **Facebook and Instagram** in one ad set by default (Meta picks Feed, Stories, Reels). Instagram needs
  an ad image: upload it on the campaign page. The Instagram account linked to the Page is found automatically, or
  set `META_INSTAGRAM_ACCOUNT_ID`. Choose Facebook-only or Instagram-only per campaign.
- Publishing resumes where it stopped if a step fails, and objects are named `<name> [milo #id]` so a timed-out
  create is found again instead of duplicated. Ids made in dry-run mode are never sent to Meta in live mode.

## Security
- Milo listens on localhost. The Host header must be in `ALLOWED_HOSTS` (blocks DNS rebinding).
- Every change request (POST) must carry the `X-Milo-Client` header, which the UI sends. A page on another site
  can't add it, so it can't press Approve for you (CSRF).
- Approve is atomic: the approval moves from pending to executing in one database step, so a double click, a
  second tab or a concurrent reject can't run it twice.
- Stuck approvals recover: if the server stops while an approval is executing, it goes back to pending at the next
  startup, and the scheduler also resets any approval executing for more than `APPROVAL_TIMEOUT_MINUTES` (default
  10). Activity logs `approval_recovered`. Approving again is safe: publishing resumes from the saved Meta ids.
- Optional `API_TOKEN`: every `/api` request then needs `Authorization: Bearer <token>` (use this if Milo is ever
  reachable beyond localhost). Error messages shown in the UI have tokens and keys masked.

## Optimization loop
Runs after every metrics pull (every `METRICS_POLL_MINUTES`) and from **Run optimizer** in the UI. Rules, on live
campaigns with at least 1,000 impressions in 7 days, compared with the account average:

| Signal | Recommendation |
| --- | --- |
| CTR below 60% of average (or below 0.6% when only one campaign is live) | Pause |
| CTR at least 120% of average and cost per click at or below average | Raise daily budget 20% (never above `MAX_DAILY_BUDGET_INR`) |
| Cost per click above 130% of average | Trim daily budget 20% (min ₹100) |

One recommendation per campaign every 6 hours, never while another request is pending, and a rejected idea is not
repeated for 24 hours. All thresholds are `OPTIMIZER_*` settings in `.env`. Milo can explain any recommendation in chat.
Recommendations are only approvals: nothing is applied without a click. When the numbers are simulated (dry run), the
reason says "based on simulated data".

## Ad images (AI generation)
Instagram ads need a picture. Milo can create it, or you upload your own (JPG/PNG).

- **Model:** **FLUX.1 [schnell]** by Black Forest Labs (Apache-2.0 licence), run on **Cloudflare Workers AI** —
  model id `@cf/black-forest-labs/flux-1-schnell`, 4 steps, square 1024×1024 JPEG. Change with `IMAGE_MODEL`.
- **Cost:** free. Workers AI gives 10,000 neurons per day; one image is about 58 neurons (~150+ images/day).
  Milo also stops at `IMAGE_DAILY_LIMIT` (default 40) per day.
- **How:** in chat Milo calls `generate_ad_image` with a scene description, or click **✨ Generate image** on the
  campaign page. Milo adds "professional advertising photograph … no text, no letters, no logos" to every prompt,
  because image models misspell words and the ad's text, headline and button carry the message.
- **Approval gate unchanged:** the image is attached to the draft only. It is uploaded to Meta when you approve
  publishing, and can't be changed once the ad exists on Meta. Every generation is logged in Activity.
- **Setup:** `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (Workers AI Read + Edit) in `.env`. Without them the
  button shows how to set it up and Milo asks you to upload an image instead.

## Click-to-Messenger ads (chat with customers)
- **What:** `destination="messenger"` makes the ad's button **Send message**: a click opens a Messenger chat with the
  Page instead of a website. Ask Milo for an ad where "people can message us".
- **On Meta:** campaign objective `OUTCOME_ENGAGEMENT`, ad set `destination_type=MESSENGER` with optimisation goal
  `CONVERSATIONS`, placements Facebook + Messenger (Instagram is left out), creative button `MESSAGE_PAGE` and an
  optional welcome message (max 300 characters). Built from Meta's Click to Messenger guide; unit-tested, not yet run
  against the live account.
- **Who answers:** the Page's own automated replies (Meta Business Suite > Inbox > Automations: instant reply,
  frequently asked questions, away message). Free, no extra server; Milo doesn't read or send messages.

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
- Cloudflare Workers AI free plan: 10,000 neurons per day (FLUX.1 [schnell] ≈ 58 per image, so ~150+ images/day).
- Meta standard access: about 300 management + 600 Insights calls per hour per ad account; 4 budget changes per ad set per hour.

## API
| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | The Milo UI (`web/index.html`) |
| POST | `/api/chat` | Chat turn, streamed as SSE events: `delta`, `message`, `tool_call`, `tool_result`, `approval`, `error`, `done` |
| GET | `/api/campaigns?status=&q=` | Campaigns, optionally filtered by status / name |
| GET | `/api/campaigns/{id}` | One campaign |
| GET | `/api/campaigns/{id}/metrics` | Stored daily metrics (last 7 days) |
| GET | `/api/approvals?status=pending` | Approvals (`pending`, `executing`, `approved`, `rejected`, `all`), with campaign name |
| POST | `/api/approvals/{id}/approve` · `/reject` | Human decision; approve executes the action (atomic; 409 if already decided, 502 + retry on Meta failure) |
| POST | `/api/metrics/refresh` | Pull metrics now (also runs the optimizer) |
| POST | `/api/optimizer/run` | Run the optimizer now; returns recommendations filed and ideas skipped (cooldown / already pending) |
| POST | `/api/campaigns/{id}/requests` | UI buttons: request publish / pause / budget change (creates an approval only) |
| GET | `/api/campaigns/{id}/approvals` | Approval history for one campaign |
| POST | `/api/campaigns/{id}/image` · GET same | Upload (JPG/PNG ≤ 8 MB) / view the ad image; sent to Meta on publish |
| POST | `/api/campaigns/{id}/generate-image` | Generate the ad image with FLUX.1 [schnell] (Cloudflare Workers AI); body `{prompt}` optional; draft only |
| GET | `/api/campaigns/{id}/preview?format=` | Read-only: Meta's rendering of the ad (Facebook mobile/desktop feed, Instagram feed/story), shown on the campaign page |
| GET | `/webhooks/meta` | Meta's webhook subscribe check: echoes `hub.challenge` when `hub.verify_token` matches `META_WEBHOOK_VERIFY_TOKEN` |
| POST | `/webhooks/meta` | Incoming Messenger / Instagram DMs; `X-Hub-Signature-256` checked with `META_APP_SECRET`; stored once per message id |
| GET | `/api/inbox` | DM conversations, newest first: unread count, last message, reply-window state |
| GET | `/api/inbox/{platform}/{page_id}/{customer_id}` | One conversation's messages (marks new ones read) |
| POST | `/api/inbox/{platform}/{page_id}/{customer_id}/reply` | Reply as the Page via Meta's Send API; body `{text}`; 409 outside the reply window |
| POST | `/api/inbox/{platform}/{page_id}/{customer_id}/suggest` | Ask the DM bot for a draft reply to the latest message (never sends) |
| GET · POST | `/api/faq` · PUT/DELETE `/api/faq/{id}` | The bot's FAQ (it answers only from these); `POST /api/faq/sample` loads the sample home-cleaning FAQ |
| GET | `/api/leads` | Contacts captured from DMs (name, phone, conversation) |
| GET | `/api/status` | Model, database check, dry-run, spend switch and caps, Meta, metrics, optimizer, research, image generation (model, used today) — no secrets |
| GET | `/api/summary` | Dashboard KPIs and daily spend (last 7 days) |
| GET | `/api/audit` | Activity log |

## MCP server (milo-meta)
Milo's tools live in an MCP server, so any MCP client can use them. The ADK agent starts it automatically.

| Tool | What it does |
| --- | --- |
| `create_campaign_draft` | Validates and saves a draft; platforms (Facebook and/or Instagram), destination (website or Messenger chat), countries or a local area (place + latitude/longitude + 1–80 km radius), budget within the cap |
| `list_campaigns` | Lists campaigns |
| `get_campaign_metrics` | Daily spend, impressions, clicks, CTR, CPC |
| `request_publish` / `request_pause` / `request_budget_change` | Creates a pending approval only — never changes a live ad |
| `list_pending_approvals` | What is waiting for the user, with the optimizer's reason for each recommendation |
| `research_topic` | Recent web context for a business/topic and city (Tavily, time-limited); results marked untrusted |
| `read_landing_page` | Trimmed text of an https landing page, so copy matches the real offer |
| `generate_ad_image` | Creates the draft's ad image with FLUX.1 [schnell] on Cloudflare Workers AI (scene only, no text); never sends anything to Meta |

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

## DM webhook (step 1 of DM handling)
Meta sends every Messenger and Instagram DM to the Page to `POST /webhooks/meta`. Milo checks the signature,
stores the message in `inbound_messages` (one row per Meta message id, so retries are dropped) and answers 200
at once. It does not reply yet: the DM agent (FAQ, lead capture, hand-off to a human) is the next step. Design:
the "Milo Next Milestone: DMs and Client Accounts" write-up on PR #2.

- **Security:** `/webhooks/` is the only path that skips the Host allow-list, because Meta calls it from the
  internet. Each event must carry `X-Hub-Signature-256` = HMAC-SHA256 of the raw body with the app secret, checked
  before parsing (401 otherwise). It has no `/api` prefix and can only store messages; `/api/*` stays localhost-only.
- **Setup (test with your own account, no App Review needed):**
  1. `.env`: `META_APP_SECRET` and `META_APP_ID` (App Dashboard > App settings > Basic) and
     `META_WEBHOOK_VERIFY_TOKEN` (any long random string you choose).
  2. Public HTTPS for local Milo, for example `cloudflared tunnel --url http://localhost:8000` (prints an
     `https://….trycloudflare.com` address). Self-signed certificates are not accepted by Meta.
  3. App Dashboard > Messenger (or Webhooks) > Callback URL `https://<tunnel>/webhooks/meta`, Verify token = the
     same string, then subscribe the Page to `messages`, `messaging_postbacks` and `message_echoes`. Without
     `message_echoes`, replies a person types in Business Suite or the Instagram app never reach Milo, and the bot
     can't tell that a person has taken the conversation. For Instagram DMs, also subscribe the `instagram` object.
  4. Message the Milo Test Page from an account that has a role on the app; the message appears in
     `inbound_messages` and Activity shows "DM received". Messages from the public need Advanced Access for
     `pages_messaging` (App Review).

## Inbox (step 2 of DM handling)
The **Inbox** page lists every Messenger / Instagram conversation with your Page and lets a person reply. Replies
go out as the Page through Meta's Send API (`POST /{page_id}/messages`) and are stored in the same thread.

- **Reply window (Meta policy):** a reply is allowed within 24 hours of the person's last message, or up to 7 days
  after their first message from a click-to-Messenger ad. After that, only a person may reply, with the
  `HUMAN_AGENT` tag, up to 7 days, and only if Meta has granted the app the Human Agent feature
  (`META_HUMAN_AGENT_ENABLED=true`). Outside these windows Milo refuses to send, and the Inbox says why.
- **Token:** replies need a Page access token with `pages_messaging`. Set `META_PAGE_ACCESS_TOKEN`, or Milo fetches
  one from the Page with `META_ACCESS_TOKEN`.
- **Dry run:** with `META_DRY_RUN=true`, replies are saved in the thread (marked "dry run, not sent") and nothing
  is sent to Meta.
- **Not yet:** automatic replies. The DM agent (FAQ, lead capture, hand-off) is step 3; it will use the same
  sending code with the same window rules and can never use the human tag.

## DM bot (step 3 of DM handling)
For every new DM the bot does one of three things, logged in Activity and shown in the Inbox:

| Message | Bot action |
| --- | --- |
| Covered by the FAQ ("Which areas do you cover?") | **Answer** built only from those FAQ entries |
| Contains an Indian mobile number or asks for a callback | **Lead** saved (`/api/leads`), receipt confirmed |
| Anything else (prices not in the FAQ, complaints, unclear) | **Hand-off**: "A team member will reply soon", thread marked "needs a person" |

- **Drafts by default:** `DM_AUTOREPLY=false` means the bot only drafts; the Inbox shows "Bot draft" with
  **Use draft**, and a person clicks Send. `DM_AUTOREPLY=true` lets it send by itself.
- **Guardrails:** separate from the ads agent, with no tools that can change ads. DM text is fenced as untrusted
  data; the model must cite FAQ ids that exist, otherwise the reply becomes a hand-off (no invented prices).
  Phone numbers are detected in code. A model error is a hand-off. It obeys Meta's reply window, never uses the
  `HUMAN_AGENT` tag, says it is automated in its first message, and stays out of a conversation once a person
  has replied.
- **FAQ:** edit it under the Inbox ("Bot FAQ"). **Load sample FAQ** adds six labelled sample answers for the demo
  home-cleaning business; replace them with the real business's words.
- **Settings:** `DM_AGENT_ENABLED` (default true), `DM_AUTOREPLY` (default false), `DM_BUSINESS_NAME`. It uses the
  same model as chat (`MILO_MODEL`).

## Future work
- **n8n notifications (optional):** call an n8n webhook to send Telegram/email alerts when an approval is waiting
  or a campaign's CPC spikes. Kept out of the core so the agent harness stays in code.
