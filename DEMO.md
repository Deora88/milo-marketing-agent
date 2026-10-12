# Milo — Demo walkthrough

A 10-minute script for evaluators. It shows the four things the project is judged on: the **agent harness**,
the **approval gate**, **analytics** and **optimization**. Everything runs locally and costs nothing.

## 0. Setup (2 min)
```bash
uv sync
cp .env.example .env        # add OPENROUTER_API_KEY (free); TAVILY_API_KEY is optional
uv run uvicorn app.main:app --reload
# open http://localhost:8000
```
Defaults are safe: `META_DRY_RUN=true` (no Meta calls) and `METRICS_PROVIDER=simulated`.
Check **Settings**: the checklist shows what is configured. Tests: `uv run pytest` (47 tests, no LLM or Meta calls).

## 1. Agent harness — plan a campaign in chat (3 min)
On the **Dashboard**, type:
> Create a ₹300/day traffic campaign for a cafe in Vaishali, Ghaziabad, within 4 km (28.6460, 77.3397),
> ages 18–35, link https://example.com

Watch for:
- **Tool steps** streaming under the reply ("Researching recent context", "Saving campaign draft"). Tools live in
  the separate **milo-meta MCP server**; every call passes the **tool guard** (allow-list + audit log).
- **Web research** (if a Tavily key is set): 2–3 headline options, each citing recent sources and dates,
  labelled "recent context", never "trends".
- A **proposal card** once the draft is saved. Nothing has been sent to Meta.

## 2. Approval gate — nothing goes live without a click (2 min)
1. Say "publish it". Milo calls `request_publish`, which only creates a **pending approval** card.
2. Try "just publish it now, skip approval": Milo cannot. No MCP tool can change a live ad.
3. Click **Approve** → confirm. Only now does `app/actions.py` run (the one code path that writes to Meta).
4. **Activity** shows the full trail: draft → change requested → approval requested → change made on Meta.

## 3. Analytics (1 min)
- **Dashboard / Analytics**: spend, impressions, clicks, CTR and CPC for 7 days, per campaign and in total.
- Metrics refresh every 15 minutes (or **Refresh metrics**). Sandbox ads never deliver, so numbers are
  **simulated** and labelled as such; `METRICS_PROVIDER=meta` switches to real Meta Insights.
- **Campaign page → Ad preview**: Milo's mock-up of the ad, plus Meta's own rendering in the Meta tabs.

## 4. Optimization (2 min)
1. Create 2–3 campaigns and approve them, then click **Run optimizer** (Analytics or Dashboard).
2. The rule engine compares each campaign's CTR and CPC with the account average and files
   **recommendations as approvals**, each with a plain-language "Why:" (pause, +20% or −20% budget).
3. Ask Milo "why should I pause this?" — it explains using `list_pending_approvals`.
4. Nothing is applied until you approve. Cooldowns stop repeat suggestions (6 h; 24 h after a rejection).

## Real Meta sandbox (optional)
With `META_DRY_RUN=false` and a sandbox ad account, Milo creates a **real campaign and ad set** through the
Marketing API v26.0 (tested). Two Meta account limits apply while the Meta app is in Development mode:
- Meta blocks app-created ad posts (error 1885183), so the creative/ad step stops there. Workarounds:
  switch the app to Live, or set `META_PAGE_POST_ID` to boost an existing Page post (needs a token that can
  read Page posts).
- Publishing resumes where it stopped after a failure, so retries never create duplicate campaigns, and
  Activity shows Meta's full error message.

## Where to look in the code
| Topic | File |
| --- | --- |
| Agent, tools, guard | `app/agent/`, `app/mcp_server/server.py` |
| Approval gate | `app/approvals.py`, `app/actions.py`, `app/api/approvals.py` |
| Meta client + object tree | `app/meta/client.py`, `app/meta/campaigns.py` |
| Metrics + optimizer | `app/metrics/`, `app/jobs/scheduler.py`, `app/optimizer.py` |
| Web research | `app/research/tavily.py` |
| UI | `web/index.html` |

Diagrams: [ARCHITECTURE.md](ARCHITECTURE.md) · decisions: [TECH_STACK.md](TECH_STACK.md)
