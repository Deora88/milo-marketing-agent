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
uv run pytest                # 200+ tests, in-memory MongoDB; ignores your .env; never calls the LLM, Meta or Tavily
```
No `uv`? `pip install uv` first (Windows: `python -m pip install uv`).

**MongoDB Atlas (free):** create an M0 cluster, add a database user, allow your IP under *Network Access*, then copy
the connection string into `MONGODB_URI`. Settings shows "Database (MongoDB): Connected" when it works.
`uv run python scripts/check_mongo.py` checks the connection on its own (Milo uses certifi's CA list for Atlas, which
fixes the TLS handshake errors some Windows PCs get).

**Slow replies?** The default `openrouter/openrouter/free` router picks a random free model, often a slow "thinking"
one. Pin one fast free model that supports tools in `.env` (`MILO_MODEL=openrouter/<provider>/<model>:free`).
The server log prints `chat turn: first response Xs, total Ys` for every reply.

## How a request flows
chat UI → `POST /api/chat` (SSE) → ADK agent `milo` → tool guard (`before_tool_callback`) →
**milo-meta MCP server** (stdio) → `request_*` tool stores a pending Approval → user clicks Approve →
`app/actions.py` calls Meta → MongoDB → dashboard.

Before writing ad copy Milo calls `research_topic` (Tavily web search, past month by default) and, if you give a URL,
`read_landing_page`. It writes one headline and one primary text, citing the sources and dates they were based on. See
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
  dm/knowledge.py    business PDFs: upload checks, text extraction (pypdf), passages the bot may cite
  dm/profiles.py     sender names from Meta (Messenger profile / Instagram user), cached
  security.py        Host allow-list, X-Milo-Client header on changes (CSRF), optional API token
  util/redact.py     masks tokens and keys in error text
  api/               routes: chat (SSE), campaigns (+ change requests), approvals, insights (status, summary, audit), webhooks (Meta DMs), inbox (DM threads + replies), faq (bot FAQ + leads), knowledge (business PDFs)
web/
  index.html         the Milo UI (single file, served at /)
  milo.css           prebuilt Tailwind styles; tokens in tailwind.config.js
tests/               201 tests: MCP tools + approval gate (caps, spend switch, CSRF, double approve, failures), MCP over
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
- **Who answers:** Milo's Inbox and DM assistant (see [DM bot](#dm-bot-step-3-of-dm-handling)), or the Page's own
  automated replies in Meta Business Suite if Milo's webhook isn't connected.

## Ad research (web search)
When you describe what to promote ("a bakery in Pune"), Milo calls `research_topic` once with the business and
city. The tool asks Tavily for pages from the past `RESEARCH_TIME_RANGE` (default `month`) and returns up to
`RESEARCH_MAX_RESULTS` sources: title, URL, published date and a short snippet. Milo then writes one headline and one
primary text, with a "based on" note. The chat shows the sources under the reply.

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
| GET · POST | `/api/knowledge` | Business PDFs for the DM bot: list, upload (multipart `file`) |
| GET · PATCH · DELETE | `/api/knowledge/{id}` | Read a PDF's extracted text page by page, turn it on/off for the bot, delete it |
| POST | `/api/inbox/{platform}/{page_id}/{customer_id}/handback` | Hand the conversation back to the DM bot (earlier hand-offs and Page replies stop counting) |
| GET · POST | `/api/faq` · PUT/DELETE `/api/faq/{id}` | The bot's FAQ (it answers only from these); `POST /api/faq/sample` loads the sample home-cleaning FAQ |
| DELETE | `/api/inbox/{platform}/{page_id}/{customer_id}` | Delete Milo's copy of a conversation, the person's leads and saved name (data deletion requests; nothing is deleted on Meta) |
| GET | `/api/leads` | Contacts captured from DMs (name, phone, conversation) |
| DELETE | `/api/leads/{id}` | Delete one lead |
| GET | `/api/leads.csv` | The same leads as a CSV file for Excel (name, phone, platform, sender id, message) |
| GET | `/api/comments` | Comments on your Instagram / Facebook posts, newest first (`?status=open`, `new`, `replied`, `hidden`) and the unread count |
| POST | `/api/comments/{id}/reply` · `/hide` · `/suggest` · `/api/comments/read` | Public reply as the business (`{text}`), hide / show (`{hidden}`), bot draft from the FAQ (never posts), mark read |
| GET | `/api/instagram/check` | Read-only check of the Meta-side setup for Instagram DMs |
| GET · POST | `/api/posts` | Instagram posts: list, create a draft (`kind`, `caption`, `scheduled_at`) |
| GET · PATCH · DELETE | `/api/posts/{id}` | One post: read, edit (draft/failed only), delete |
| POST · DELETE | `/api/posts/{id}/images`, `/generate-image`, `/images/{i}` | Add (upload or AI) / remove images |
| POST | `/api/posts/{id}/request` | Request approval to publish (formats media, builds the Reel video) |
| POST | `/api/posts/{id}/insights/refresh` | Fetch a published post's latest results from Instagram (read-only) |
| GET | `/media/{token}/{file}` | Public, read-only: post media for Meta's downloader |
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
at once. The [Inbox](#inbox-step-2-of-dm-handling) shows it and the [DM bot](#dm-bot-step-3-of-dm-handling)
handles it in the background.

- **Security:** `/webhooks/` is the only path that skips the Host allow-list, because Meta calls it from the
  internet. Each event must carry `X-Hub-Signature-256` = HMAC-SHA256 of the raw body with the app secret, checked
  before parsing (401 otherwise). It has no `/api` prefix and can only store messages; `/api/*` stays localhost-only.
- **Setup (test with your own account, no App Review needed):**
  1. `.env`: `META_APP_SECRET` and `META_APP_ID` (App Dashboard > App settings > Basic) and
     `META_WEBHOOK_VERIFY_TOKEN` (any long random string you choose).
  2. Public HTTPS for local Milo: `start-milo.ps1` starts Milo and an ngrok tunnel on the team's fixed ngrok
     domain (needs ngrok 3.28 or newer; change `$domain` in the script for your own); `cloudflared tunnel --url http://localhost:8000` also works. Put the same
     address in `PUBLIC_BASE_URL` (Instagram posts need it). Self-signed certificates are not accepted by Meta.
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
- **Automatic replies** come from the [DM bot](#dm-bot-step-3-of-dm-handling): same sending code, same window
  rules, and it can never use the human tag.

## Instagram DMs
Instagram Direct messages work like Messenger ones (Inbox, bot, leads, names), through the **Messenger API for
Instagram** with the Facebook Page: no separate Instagram login or keys.

- **How it works:** Meta sends Instagram DMs to the same `/webhooks/meta` as `"object": "instagram"`; `entry.id`
  is the Instagram account id. Replies go out through the **linked Facebook Page**: `POST /{META_PAGE_ID}/messages`
  with the Page token. Milo checks the DM went to the Instagram account linked to that Page
  (`META_INSTAGRAM_ACCOUNT_ID`, or asked from Meta once) and refuses otherwise.
- **Limits:** Instagram replies must be under 1,000 characters (Hindi letters count as 3 bytes); the bot sends
  FAQ answers only if they fit, otherwise it hands off. Same 24-hour window; `HUMAN_AGENT` works the same way.
- **Names:** `name` and `@username` from Meta (`GET /{IGSID}?fields=name,username`, Page token).
- **Setup:**
  1. Link the Instagram professional account to the Facebook Page (Meta Business Suite > Settings > Accounts >
     Instagram accounts).
  2. Instagram app > Settings > Messages and story replies > Message controls > Connected tools > turn on
     **Allow access to messages**.
  3. App Dashboard > Webhooks > **Instagram**: Callback URL = your `https://…/webhooks/meta`, the same verify
     token, subscribe to `messages` and `messaging_postbacks`.
  4. Token permissions: `instagram_basic`, `instagram_manage_messages`, `pages_manage_metadata`,
     `pages_messaging`. The app must be published (Live) to receive webhooks; with Standard Access only people
     with a role on the app can DM it.
  5. **Settings > Check Instagram DM setup** (read-only, `GET /api/instagram/check`) shows what is still
     missing: Page link, Page subscriptions, the app's `instagram` webhook, and whether a DM has arrived.

## Instagram posts (photo, carousel, Reel, Story)
The **Posts** page creates organic posts for the Instagram account linked to your Page (no budget, not ads), with
the same approval gate as ads: nothing is posted until you click **Approve**.

- **Types:** Photo (1 image), Carousel (2–10), **Reel** (1–10 images turned into a 9:16 video, 3 s per image, made
  with the ffmpeg bundled in `imageio-ffmpeg`), **Story** (1 image, 9:16, disappears after 24 hours, no caption).
- **Images:** upload JPG/PNG or **✨ Generate** with FLUX. Milo stores JPEGs and formats them per type when you
  request publishing (feed: 4:5 to 1.91:1; Stories/Reels: 1080×1920 over a blurred background).
- **Caption checks:** 2,200 characters, 30 hashtags, 20 @mentions (Instagram's limits).
- **Text on image (Stories and Reels):** headline (60), details (90) and a button-style call to action (30)
  drawn onto the 9:16 frame above and below the picture, in Plus Jakarta Sans (bundled in `app/social/fonts`,
  SIL Open Font License) and the brand orange. Instagram's API can't add text or stickers, so the text is part
  of the image; the preview shows the exact frame. Emoji are dropped (the font can't draw them).
- **Scheduling:** optional time (at least 5 minutes, at most 60 days ahead). After approval the post waits as
  *Scheduled*; the scheduler posts it at that time (Milo must be running). If Milo was off for more than 6 hours
  past that time, the post is marked *failed* ("Missed") instead of going out late; pick a new time and request again.
- **Chat:** "Make a Reel about our ceramic coating" → Milo drafts it (`create_instagram_post`) and requests
  approval (`request_post_publish`). It can never post by itself.
- **How it posts:** Instagram Content Publishing API through the Page token: `POST /{ig-user-id}/media` per item
  (carousel children, `REELS`, `STORIES`), waits for `status_code=FINISHED`, then `POST /{ig-user-id}/media_publish`
  and shows **View on Instagram**. Meta downloads the media from `PUBLIC_BASE_URL/media/<random token>/<file>`:
  the only other public route besides `/webhooks/`, read-only, and only for a post waiting to be published.
- **Setup:** set `PUBLIC_BASE_URL=https://<your ngrok domain>` in `.env`, and add `instagram_content_publish`
  to the token (App Dashboard > Use cases > Manage messaging & content on Instagram > Permissions). Dry run
  marks posts published without contacting Instagram.
- **Limits:** about 50–100 API posts per 24 hours per account; no music, filters, stickers or shopping tags
  through the API.
- **Results:** published posts show likes, comments, views, reach, saves, shares and interactions (Reels also
  average watch time; Stories views, reach, replies, shares, follows, profile visits) with a **Refresh** button.
  Milo also refreshes them by itself: Stories every 15 minutes while live (Instagram keeps Story numbers for 24
  hours only), posts and Reels hourly for a week. Likes and comments are plain media fields; the rest needs the
  `instagram_manage_insights` permission. Instagram doesn't share Story likes, and hides numbers under 5.
- **Story replies and mentions** arrive as DMs and show in the Inbox as "Replied to your story" or "Mentioned
  you in their story" (with a link while Meta's story link is valid).

## Comments (Instagram and Facebook posts)
The **Comments** page lists public comments on your Instagram posts and Facebook Page posts (Milo's own posts are
linked), newest first, with **To answer / Replied / Hidden / All** tabs and an unread count in the sidebar.

- **A person replies, the bot only drafts:** a comment is public, so nothing is posted until you click **Reply**.
  **Ask the bot for a draft** uses the same FAQ/PDF rules as DMs; if a comment has a phone number, the draft asks
  the person to DM you and suggests hiding the comment. **Hide** keeps a comment visible only to the writer and their
  friends (spam, phone numbers). Dry run saves replies in Milo only.
- **How it works:** the same `/webhooks/meta` receives `entry[].changes[]`: Instagram field `comments`, and Page
  field `feed` (only `item: comment`; edits, deletes and hides are applied too). Replies go out with the Page token:
  Instagram `POST /{comment-id}/replies` (under the top comment), Facebook `POST /{comment-id}/comments`; hiding is
  `POST /{comment-id}` with `hide` (Instagram) or `is_hidden` (Facebook).
- **Setup:** App Dashboard > Webhooks: subscribe the **Instagram** object to `comments` and the **Page** object to
  `feed`; add `instagram_manage_comments`, `pages_read_user_content` and `pages_manage_engagement` to the token.
  Comments from the public need those permissions approved in App Review; testers work before that.

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
  `HUMAN_AGENT` tag and says it is automated in its first message. With auto-reply on it sends the stored FAQ
  answers word for word, whole entries only (never cut mid-sentence); if they don't fit one message it hands off.
- **When the bot stays out of a conversation:**
  - a person replied as the Page (Milo Inbox, Business Suite, Instagram app): until someone clicks
    **Hand back to bot** in the Inbox (`POST /api/inbox/{platform}/{page_id}/{customer_id}/handback`);
  - the bot handed a message to a person: for `DM_HANDOFF_HOURS` (default 24) after that message, then it
    answers FAQ questions again (or click **Hand back to bot** sooner);
  - a technical failure (Meta timeout, database error) does **not** keep it out: the message is marked "failed"
    and the conversation shows "needs a person", but the bot still answers the customer's next message.
  The Inbox shows why the bot is staying out, including the app id of a Page reply from another app.
- **Never stuck:** one model reply may take at most 30 seconds (free OpenRouter models sometimes never answer;
  then the message goes to a person), and a whole bot run at most 120 seconds. Every 2 minutes Milo marks any
  message left on "working on it" for over 5 minutes as failed, so a person sees it and **Ask the bot for a
  draft** works again. The Inbox refreshes by itself while the bot is working. For faster, steadier replies, pin
  one free non-reasoning model with `MILO_MODEL` instead of `openrouter/openrouter/free` (see `.env.example`).
- **After a restart:** DMs that arrived but were never handled (last 24 hours) are run again as drafts only,
  even with `DM_AUTOREPLY=true`, so Milo never auto-sends replies to hours-old messages.
- **FAQ:** edit it under the Inbox ("Bot FAQ"). **Load sample FAQ** adds six labelled sample answers for the demo
  home-cleaning business; replace them with the real business's words.
- **Business documents (PDF):** under the Inbox, **Upload PDF** (price list, brochure, policies; PDF only, up to
  10 MB and 100 pages). Milo keeps only the extracted text, page by page; **Read** shows exactly what the bot sees,
  **Turn off** hides a document from the bot, **Delete** removes it. Scanned PDFs (no text layer) are refused:
  there is no OCR. For each DM the bot is shown the few passages that share the most words with the message, with
  ids it must cite (checked in code, otherwise a hand-off). **An answer that uses a PDF is always a draft**, even
  with `DM_AUTOREPLY=true`: a person checks it and clicks Send. The draft notes the PDF and page it came from.
- **Leads:** the **Leads** card at the top of the Inbox lists every phone number people shared in DMs (newest
  first) with the sender's name and id, the message, the time and **Open chat**. **Download CSV** saves them all
  for Excel; cells that look like formulas are saved as text so the file can't run anything.
- **Sender names:** the Inbox shows the sender's name and full id (PSID / Instagram id). Milo asks Meta once per
  person with the Page token (Messenger: `first_name,last_name`; Instagram: `name,username`) and caches it. If
  Meta shares no name (privacy settings, missing permission), it shows "Unknown sender" and asks again after a
  day. Dry run never calls Meta.
- **Settings:** `DM_AGENT_ENABLED` (default true), `DM_AUTOREPLY` (default false), `DM_HANDOFF_HOURS`
  (default 24), `DM_BUSINESS_NAME`. It uses the
  same model as chat (`MILO_MODEL`).

## Troubleshooting
| What you see | Cause and fix |
| --- | --- |
| Chat: `cannot import name 'CacheableResult' from 'mcp.types'` | litellm 1.105+ is installed. Stop Milo, run `uv sync` (the lock pins 1.104). |
| `uv sync`: "being used by another process" | Milo (or VS Code's Python) still runs from `.venv`. Close it, then `uv sync` again. |
| Many tests fail with your own settings (`DM_AUTOREPLY=true`...) | Old test setup: `tests/conftest.py` now clears Milo settings and stops litellm loading `.env`. |
| Bot: `model unavailable (no reply within 30s)` | A slow free model. Try again, or pin one fast free non-reasoning model in `MILO_MODEL`. |
| Comments page stays empty | Look at the Milo window's `webhook:` line after a new comment. No line or no `changes comments` = Meta isn't sending them: subscribe the Instagram `comments` field, and in Development mode comment from an Instagram Tester account. `from your own account` = it's under **All**. |
| Reel or Story: "media download failed" | ngrok isn't running or `PUBLIC_BASE_URL` isn't your ngrok https address. |
| Atlas: TLS / SSL handshake error | Run `uv run python scripts/check_mongo.py`; check `MONGODB_URI` and Atlas Network Access (your IP). |

## Future work
- **n8n notifications (optional):** call an n8n webhook to send Telegram/email alerts when an approval is waiting
  or a campaign's CPC spikes. Kept out of the core so the agent harness stays in code.
