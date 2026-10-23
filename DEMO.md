# Milo — Demo walkthrough

A 10-minute script for evaluators. It shows the four things the project is judged on: the **agent harness**,
the **approval gate**, **analytics** and **optimization**. Everything runs locally and costs nothing.

## 0. Setup (2 min)
```bash
uv sync
cp .env.example .env        # MONGODB_URI (Atlas) + OPENROUTER_API_KEY (free); TAVILY_API_KEY and
                            # CLOUDFLARE_ACCOUNT_ID/CLOUDFLARE_API_TOKEN (AI images) are optional
uv run uvicorn app.main:app --reload
# open http://localhost:8000
```
Defaults are safe: `META_DRY_RUN=true` (no Meta calls, simulated metrics) and `META_ALLOW_SPEND=false`.
Check **Settings**: the checklist shows the database connection, spend switch, caps and what is configured.
Tests: `uv run pytest` (85 tests, in-memory MongoDB, no LLM, Meta or Tavily calls).

## 1. Agent harness — plan a campaign in chat (3 min)
On the **Dashboard**, type:
> Create a ₹200/day traffic campaign for a cafe in Vaishali, Ghaziabad, within 4 km (28.6460, 77.3397),
> ages 18–35, link https://example.com

Watch for:
- **Tool steps** streaming under the reply ("Researching recent context", "Saving campaign draft"). Tools live in
  the separate **milo-meta MCP server**; every call passes the **tool guard** (allow-list + audit log).
- **Web research** (if a Tavily key is set): 2–3 headline options, each citing recent sources and dates,
  labelled "recent context", never "trends".
- A **proposal card** once the draft is saved. Nothing has been sent to Meta.
- Ask Milo to create the picture, or open the campaign and click **✨ Generate image**: Milo uses **FLUX.1 [schnell]**
  on Cloudflare Workers AI (`@cf/black-forest-labs/flux-1-schnell`, free) and attaches a square photo with no text to
  the draft. You can regenerate or **Upload ad image** instead (the ad runs on Facebook + Instagram; Instagram needs
  a picture). Nothing goes to Meta until publish is approved.

## 2. Approval gate — nothing goes live without a click (2 min)
1. Say "publish it". Milo calls `request_publish`, which only creates a **pending approval** card. The card shows
   exactly what goes live: platforms, budget, area, ages, headline, text and link.
2. Try "just publish it now, skip approval": Milo cannot. No MCP tool can change a live ad.
3. Click **Approve** → confirm. Only now does `app/actions.py` run (the one code path that writes to Meta).
4. **Activity** shows the full trail: draft → change requested → approval requested → change made on Meta.
5. Security checks (also covered by tests): a request from another site without the `X-Milo-Client` header is
   refused (403), a second Approve click gets "already approved" (409), and budgets above the cap are refused.

## 3. Analytics (1 min)
- **Dashboard / Analytics**: spend, impressions, clicks, CTR and CPC for 7 days, per campaign and in total.
- Metrics refresh every 15 minutes (or **Refresh metrics**). In dry run the numbers are **simulated** and labelled
  as such; live campaigns use real Meta Insights (paused ads have no new results).
- **Campaign page → Ad preview**: Milo's mock-up of the ad, plus Meta's own rendering in the Meta tabs.

## 4. Optimization (2 min)
1. Create 2–3 campaigns and approve them, then click **Run optimizer** (Analytics or Dashboard).
2. The rule engine compares each campaign's CTR and CPC with the account average and files
   **recommendations as approvals**, each with a plain-language "Why:" (pause, +20% or −20% budget, never above
   the cap). Reasons based on simulated data say so.
3. Ask Milo "why should I pause this?" — it explains using `list_pending_approvals`.
4. Nothing is applied until you approve. Cooldowns stop repeat suggestions (6 h; 24 h after a rejection).

## Live Meta account (Facebook + Instagram)
Set `META_DRY_RUN=false` with the business account's system-user token, ad account and Page.
- **Spending stays off** (`META_ALLOW_SPEND=false`): Approve creates the campaign, ad set, image, creative and ad on
  Meta, but everything stays **PAUSED**, so nothing is shown and nothing is charged. Check it in Ads Manager.
- Pause and budget edits work for real; **resume** is refused until the account owner sets `META_ALLOW_SPEND=true`.
- Hard caps: ₹500/day per campaign and ₹1,000/day in total by default (`MAX_DAILY_BUDGET_INR`,
  `MAX_TOTAL_DAILY_BUDGET_INR`).
- Publishing resumes where it stopped after a failure, objects are named `<name> [milo #id]` so a timed-out create
  is found instead of duplicated, and Activity shows Meta's full error message (with secrets masked).

## Where to look in the code
| Topic | File |
| --- | --- |
| Agent, tools, guard | `app/agent/`, `app/mcp_server/server.py` |
| Security (CSRF, Host, token) | `app/security.py` |
| Database | `app/db/` (MongoDB) |
| Approval gate | `app/approvals.py`, `app/actions.py`, `app/api/approvals.py` |
| Meta client + object tree | `app/meta/client.py`, `app/meta/campaigns.py` |
| Metrics + optimizer | `app/metrics/`, `app/jobs/scheduler.py`, `app/optimizer.py` |
| Web research | `app/research/tavily.py` |
| Ad images (FLUX.1 schnell, Cloudflare) | `app/creative/imagegen.py` |
| UI | `web/index.html` |

Diagrams: [ARCHITECTURE.md](ARCHITECTURE.md) · decisions: [TECH_STACK.md](TECH_STACK.md)
