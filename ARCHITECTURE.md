# Milo — Architecture

Milo is one FastAPI process plus one MCP server subprocess. The FastAPI app holds the **chat agent** (Google ADK),
the **approvals API**, the **Meta client** and a **metrics job**. The agent's tools live in the **milo-meta MCP
server**, which ADK starts over stdio. The same FastAPI process serves the UI (`web/`) at `/`, so the browser talks
only to its own origin; the backend talks to OpenRouter (LLM) and Meta (ads). Nothing that changes a live ad runs
without a human click.

> Diagrams use Mermaid. They render on GitHub, in VS Code (Markdown Preview Mermaid Support extension) and in most
> Markdown viewers.

## 1. System overview

```mermaid
flowchart LR
    subgraph Browser["Browser: Milo UI (web/index.html, served at /)"]
        UI_CHAT["Dashboard chat panel"]
        UI_CARD["Approval cards + Approvals page"]
        UI_DASH["Dashboard KPIs, Campaigns, Analytics, Activity, Settings"]
    end

    subgraph Backend["FastAPI process (app/main.py)"]
        API_CHAT["POST /api/chat (SSE)"]
        API_APPR["/api/approvals"]
        API_CAMP["/api/campaigns (+ /requests)"]
        API_INS["/api/status, /api/summary, /api/audit"]
        API_METR["POST /api/metrics/refresh"]

        subgraph Agent["ADK agent (app/agent/)"]
            RUNNER["Runner + session"]
            MILO["LlmAgent 'milo'"]
            GUARD["Tool guard (before_tool_callback): allow-list + audit"]
            TOOLSET["McpToolset (stdio client)"]
        end

        ACTIONS["actions.py: executes approved actions"]
        METACLIENT["Meta client: throttle, retry, dry-run"]
        PROVIDER["Metrics provider: simulated or Meta Insights"]
        JOB["APScheduler: pull metrics every 15 min"]
        OPT["optimizer.py: rules -> recommendations"]
    end

    subgraph MCP["milo-meta MCP server (subprocess, app/mcp_server/)"]
        T_READ["create_campaign_draft, list_campaigns, get_campaign_metrics"]
        T_REQ["request_publish, request_pause, request_budget_change"]
        T_WEB["research_topic, read_landing_page"]
    end

    TAVILY["Tavily Search / Extract API (untrusted web content)"]

    DB[("SQLite: Campaign, Approval, MetricSnapshot, AuditLog")]
    LLM["OpenRouter free models (via LiteLLM)"]
    META["Meta Marketing API v26.0"]
    OTHER["Other MCP clients (Claude Desktop, VS Code)"]

    UI_CHAT --> API_CHAT --> RUNNER --> MILO
    MILO <--> LLM
    MILO --> GUARD --> TOOLSET
    TOOLSET -- "MCP over stdio" --> T_READ
    TOOLSET -- "MCP over stdio" --> T_REQ
    OTHER -. "MCP" .-> MCP
    T_READ --> DB
    T_REQ -- "pending Approval only" --> DB
    TOOLSET -- "MCP over stdio" --> T_WEB
    T_WEB -- "recent results, trimmed" --> TAVILY
    T_WEB -- "audit only" --> DB
    UI_CARD --> API_APPR --> ACTIONS --> METACLIENT --> META
    ACTIONS --> DB
    JOB --> PROVIDER
    API_METR --> PROVIDER
    PROVIDER -. "METRICS_PROVIDER=meta" .-> METACLIENT
    PROVIDER --> DB
    JOB --> OPT
    OPT -- "pending Approval + reason" --> DB
    UI_DASH --> API_CAMP --> DB
    UI_DASH --> API_INS --> DB
```

**Key idea:** the LLM decides *what* to do, but code decides *whether* it happens. The MCP server is
**request-only**: its `request_*` tools store a pending Approval and never call Meta. Only `actions.py`, triggered by
the dashboard's Approve button, changes a live ad — whichever MCP client made the request.

## 2. Complete working flow: create → approve → publish → monitor

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as Dashboard (web/)
    participant API as FastAPI
    participant AG as ADK agent "milo"
    participant LLM as OpenRouter LLM
    participant GD as Tool guard
    participant MCP as milo-meta MCP server
    participant TAVILY as Tavily API
    participant DB as SQLite
    participant ACT as actions.py
    participant MC as Meta client
    participant META as Meta Marketing API
    participant JOB as Metrics job

    Note over AG,MCP: On first chat: McpToolset starts the MCP server (stdio) and lists its 9 tools

    Note over U,DB: Phase 1 · Plan and draft
    U->>UI: "Create a Rs 500/day campaign for my bakery in Pune"
    UI->>API: POST /api/chat {session_id, message}
    API->>AG: run_async(message)
    AG->>LLM: prompt + tool schemas
    LLM-->>AG: call research_topic("bakery", "Pune")
    AG->>GD: before_tool_callback
    GD-->>AG: allowed
    AG->>MCP: tools/call research_topic
    MCP->>TAVILY: POST /search (time_range=month, max 5 results)
    TAVILY-->>MCP: titles, URLs, dates, snippets
    MCP->>DB: AuditLog "web_research"
    MCP-->>AG: sources marked untrusted (or "unavailable" with no key)
    AG->>LLM: sources as information only
    LLM-->>AG: 2-3 headline options with "based on" notes + ask for missing details (URL, ages, category)
    AG-->>UI: SSE "delta" chunks as the reply is written, then "message" - UI lists the sources (reasoning parts are dropped)
    U->>UI: answers
    UI->>API: POST /api/chat
    AG->>LLM: prompt + history
    LLM-->>AG: call create_campaign_draft(...)
    AG->>GD: before_tool_callback
    GD-->>AG: allowed
    AG->>MCP: tools/call create_campaign_draft
    MCP->>DB: validate + save Campaign (status = draft)
    MCP-->>AG: {status: draft_saved, campaign_id}
    AG-->>UI: SSE "tool_call", "tool_result", "message: draft saved"

    Note over U,DB: Phase 2 · Approval request
    U->>UI: "Looks good, publish it"
    UI->>API: POST /api/chat
    AG->>LLM: prompt + history
    LLM-->>AG: call request_publish(campaign_id)
    AG->>GD: before_tool_callback
    GD->>DB: AuditLog "change_requested_by_agent"
    GD-->>AG: allowed
    AG->>MCP: tools/call request_publish
    MCP->>DB: check status rules + duplicates, insert Approval (pending), Campaign.status = pending_approval
    MCP-->>AG: {status: pending_approval, approval_id, summary}
    Note right of MCP: Nothing is sent to Meta.
    API->>DB: runner picks up new pending Approval
    AG-->>UI: SSE "approval" card + "message: please review"

    Note over U,META: Phase 3 · Human decision and publish
    alt User clicks Approve
        U->>UI: Approve
        UI->>API: POST /api/approvals/{id}/approve
        API->>ACT: execute(approval)
        ACT->>MC: create campaign, ad set, creative, ad (all PAUSED)
        MC->>META: POST /act_{id}/campaigns, /adsets, /adcreatives, /ads
        META-->>MC: object IDs + usage headers
        ACT->>MC: set status ACTIVE
        MC->>META: POST /{ad_id}, /{adset_id}, /{campaign_id} status=ACTIVE
        ACT->>DB: save Meta IDs, Campaign.status = active, AuditLog
        API-->>UI: approval approved + campaign
    else User clicks Reject
        U->>UI: Reject
        UI->>API: POST /api/approvals/{id}/reject
        API->>DB: Approval = rejected, Campaign.status = draft
    end

    Note over U,JOB: Phase 4 · Monitor and optimise
    loop every 15 minutes (or POST /api/metrics/refresh)
        JOB->>DB: find active/paused campaigns
        JOB->>JOB: provider.daily(campaign, 7 days)
        Note right of JOB: simulated in sandbox, GET /{campaign_id}/insights when METRICS_PROVIDER=meta
        JOB->>DB: replace MetricSnapshot rows
        JOB->>DB: optimizer: compare CTR / CPC with account average
        JOB->>DB: file recommendation as pending Approval (source=optimizer, reason)
    end
    UI->>API: GET /api/approvals
    API-->>UI: optimizer cards with "Why:" (back to Phase 3 on Approve)
    UI->>API: GET /api/campaigns/{id}/metrics
    API-->>UI: daily spend, impressions, clicks, CTR, CPC (+ "simulated" label)
    U->>UI: "How is it doing? Should I change anything?"
    AG->>MCP: tools/call get_campaign_metrics
    LLM-->>AG: suggest request_budget_change or request_pause
    AG->>MCP: tools/call request_budget_change
    MCP->>DB: new pending Approval
    AG-->>UI: approval card (back to Phase 3)
```

## 3. Campaign lifecycle

```mermaid
stateDiagram-v2
    [*] --> draft: create_campaign_draft (validated)
    draft --> pending_approval: request_publish
    pending_approval --> draft: user rejects
    pending_approval --> active: user approves (Meta objects created, set ACTIVE)
    active --> paused: request_pause approved
    paused --> active: request_publish approved
    active --> active: request_budget_change approved
```

## 4. Approval gate logic

The gate has three layers: the **tool guard** in the agent, the **request-only MCP tools**, and the **human
decision** in the dashboard.

```mermaid
flowchart TD
    A["LLM (or another MCP client) requests a tool"] --> G{"Tool guard: name in allow-list?"}
    G -- No --> X["Blocked; message returned to LLM"]
    G -- Yes --> B{"request_* tool?"}
    B -- No --> C["Run normally on MCP server: draft, list, metrics"]
    B -- Yes --> AU["AuditLog: change_requested_by_agent"] --> D{"Campaign exists and status allows it?"}
    D -- No --> E["Error returned to LLM"]
    D -- Yes --> DUP{"Same request already pending?"}
    DUP -- Yes --> P["Return existing approval"]
    DUP -- No --> F["Store Approval (pending) + summary + AuditLog"]
    F --> H["Dashboard shows approval card"]
    P --> H
    H --> I{"User decision"}
    I -- Approve --> J["actions.execute: Meta calls via throttled client"]
    I -- Reject --> K["Approval rejected; campaign back to draft"]
    J --> L["Campaign updated + AuditLog"]
```

| Action | Allowed from status |
| --- | --- |
| `request_publish` | `draft`, `paused` |
| `request_pause` | `active` |
| `request_budget_change` | `active`, `paused` (new budget ≠ current, ≥ Rs 100) |

## 4b. Optimization loop

```mermaid
flowchart TD
    T["Every metrics pull (15 min) or 'Run optimizer'"] --> S["Collect 7-day stats per live campaign from MetricSnapshot"]
    S --> E{"At least 1,000 impressions?"}
    E -- No --> X["Skip: not enough data"]
    E -- Yes --> A["Compare with account average CTR and CPC"]
    A --> R1{"CTR below 60% of average?"}
    R1 -- Yes --> P["Recommend pause"]
    R1 -- No --> R2{"CTR 120%+ of average and CPC at or below average?"}
    R2 -- Yes --> U["Recommend +20% budget (max Rs 5,000)"]
    R2 -- No --> R3{"CPC above 130% of average?"}
    R3 -- Yes --> D["Recommend -20% budget (min Rs 100)"]
    R3 -- No --> N["No change"]
    P --> C{"Cooldown: pending request, recent recommendation, or rejected in 24h?"}
    U --> C
    D --> C
    C -- Yes --> K["Skip and report why"]
    C -- No --> AP["Pending Approval: source=optimizer, reason with the numbers"]
    AP --> UI["Approval card with 'Why:' in Dashboard and Approvals"]
```

The rules live in `analyze()`, a pure function with no database access, so they are unit-tested directly. With only
one live campaign there is no average to compare against, so the loop only checks a CTR floor (0.6%).

## 5. Meta client: rate limits and safety

```mermaid
flowchart TD
    R["request(method, path)"] --> DRY{"META_DRY_RUN?"}
    DRY -- Yes --> FAKE["Log request, return fake id 'dry_...'"]
    DRY -- No --> SLOW{"Last usage >= 75%?"}
    SLOW -- Yes --> WAIT["Pause 1s"] --> SEND
    SLOW -- No --> SEND["Send to graph.facebook.com/v26.0 with Bearer token"]
    SEND --> HDR["Read usage headers: X-Business-Use-Case-Usage, X-Ad-Account-Usage, X-FB-Ads-Insights-Throttle"]
    HDR --> ERR{"Error in response?"}
    ERR -- No --> OK["Return JSON"]
    ERR -- "Rate limit (4, 17, 613, 80000-80014)" --> BACK["Exponential backoff 2s, 4s, 8s, 16s"] --> SEND
    ERR -- Other --> RAISE["Raise MetaAPIError; approval fails with 502, logged"]
```

## 6. Meta object tree created on publish

```mermaid
flowchart TD
    ACC["Ad account act_{id} (sandbox)"] --> CAMP["Campaign: objective, special_ad_categories, PAUSED"]
    CAMP --> ADSET["Ad set: daily_budget (paise), targeting (countries, ages), LINK_CLICKS, PAUSED"]
    PAGE["Facebook Page (META_PAGE_ID)"] --> CRE["Ad creative: headline, text, link, CTA"]
    ADSET --> AD["Ad: ad set + creative, PAUSED"]
    CRE --> AD
    AD -. "after approval" .-> LIVE["Status set to ACTIVE: ad, ad set, campaign"]
```

## 7. Code map

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app, lifespan (DB init + scheduler), serves `web/` |
| `app/config.py` | Typed settings from `.env` |
| `app/mcp_server/server.py` | **milo-meta MCP server** (FastMCP, stdio): draft validation, list, metrics, `request_*` tools, `research_topic` + `read_landing_page` |
| `app/approvals.py` | Creates pending approvals: status rules, duplicate check, audit, `reason` and `source` |
| `app/research/tavily.py` | Tavily client: `search_recent()` (time-limited search, http(s) only, trimmed snippets) and `extract_page()` (https landing page, trimmed); `ResearchUnavailable` when off, no key or out of credits |
| `app/optimizer.py` | Rule-based optimization loop: `analyze()` (pure rules) + `run_optimizer()` (cooldowns, files approvals) |
| `app/actions.py` | Executes approved actions against Meta — the only code that writes to Meta |
| `app/agent/agent.py` | Builds `LlmAgent` with LiteLLM model and `McpToolset` (starts the MCP server) |
| `app/agent/guard.py` | `tool_guard` — the `before_tool_callback` (allow-list + audit) |
| `app/agent/prompts.py` | Milo's system instruction |
| `app/agent/runner.py` | ADK `Runner` + sessions; converts events to SSE dicts; picks up new approvals |
| `app/meta/client.py` | Throttled httpx client: usage headers, backoff, dry-run |
| `app/meta/campaigns.py` | Builds campaign → ad set → creative → ad (resumes after a half-way failure; optional existing Page post as creative); status and budget updates; `get_preview_src()` for the read-only ad preview |
| `app/metrics/` | `MetricsProvider` interface, simulated + Meta Insights providers |
| `app/jobs/scheduler.py` | APScheduler metrics pull |
| `app/db/` | SQLModel tables + session + audit helper |
| `app/api/` | Routes: chat (SSE), campaigns (+ change requests), approvals, insights (status, summary, audit) |
| `web/index.html` | The Milo UI: dashboard + chat, campaigns, approvals, analytics, activity, settings (hash routes, calls `/api/*` on the same origin) |
| `web/milo.css` | Prebuilt Tailwind styles (`web/tailwind.config.js` holds the design tokens) |
| `tests/` | MCP tools + approvals, MCP over real stdio, HTTP API, chat event streaming, Meta client, optimizer, Tavily research (respx) |

### Process view

```mermaid
flowchart LR
    U["uvicorn app.main:app"] --> P1["FastAPI process: API, ADK agent, actions, metrics job"]
    P1 -- "spawns on first chat (stdio)" --> P2["python -m app.mcp_server.server"]
    P1 --> DB[("milo.db")]
    P2 --> DB
    CD["Claude Desktop (optional)"] -- "spawns its own copy" --> P3["python -m app.mcp_server.server"]
    P3 --> DB
```
Both processes share the same SQLite file (`DATABASE_URL`), so approvals created through any MCP client show up in
the dashboard.

## 7b. Frontend (web/)

One HTML file with vanilla JS, styled by prebuilt Tailwind (`web/milo.css`) and drawn with hand-written SVG charts.
FastAPI serves it at `/`, so it calls `/api/*` on the same origin: no CORS, no separate hosting, no Node at runtime.

| Route (hash) | Screen | Reads | Writes |
| --- | --- | --- | --- |
| `#dashboard` | Agent status, recent activity, quick actions · daily spend chart + **chat with Milo** · performance, approvals waiting, campaigns, agent insight | `/api/status`, `/api/summary`, `/api/approvals`, `/api/campaigns`, `/api/audit` | `POST /api/chat` (SSE), approve / reject, `POST /api/metrics/refresh` |
| `#campaigns` | Campaign list with search and status filters | `/api/campaigns?status=&q=` | — |
| `#campaign/{id}` | Targeting, Meta IDs, ad copy, performance chart, approval history | `/api/campaigns/{id}`, `/metrics`, `/approvals` | `POST /api/campaigns/{id}/requests` (publish / pause / budget) |
| `#approvals` | Waiting / approved / rejected / all | `/api/approvals?status=` | approve / reject |
| `#analytics` | 7-day totals, spend + clicks chart, per-campaign table, grounded analysis | `/api/summary`, per-campaign metrics | — |
| `#activity` | Audit log | `/api/audit` | — |
| `#settings` | Setup checklist and harness rules | `/api/status` | — |

Chat details: replies stream in (`delta`), each tool call shows a step, a **campaign proposal card** appears when
`create_campaign_draft` saves a draft, and approval cards render inline. Approve always opens a confirmation dialog
because it changes a live ad. The "agent insight" and "grounded analysis" text is computed in the browser from stored
metrics (fact → calculation → suggestion); the suggestion is never applied without an approval.

Design tokens (warm paper palette, Fraunces headings, Outfit body) live in `web/tailwind.config.js`; rebuild
`web/milo.css` only after changing classes in `index.html`.

## 8. Data model

```mermaid
erDiagram
    CAMPAIGN ||--o{ APPROVAL : "has"
    CAMPAIGN ||--o{ METRICSNAPSHOT : "has"
    CAMPAIGN {
        int id PK
        string name
        string objective
        string special_ad_category
        int daily_budget "paise"
        json countries
        int age_min
        int age_max
        string headline
        string primary_text
        string link_url
        string call_to_action
        string status "draft, pending_approval, active, paused"
        string meta_campaign_id
        string meta_adset_id
        string meta_creative_id
        string meta_ad_id
    }
    APPROVAL {
        int id PK
        int campaign_id FK
        string action "publish_campaign, pause_campaign, update_budget"
        json payload
        string summary
        string reason "why (optimizer numbers)"
        string source "agent, optimizer, user"
        string status "pending, approved, rejected"
        datetime decided_at
    }
    METRICSNAPSHOT {
        int id PK
        int campaign_id FK
        string date
        float spend
        int impressions
        int reach
        int clicks
        float ctr
        float cpc
        string source "simulated, meta"
    }
    AUDITLOG {
        int id PK
        string event
        json detail
        datetime created_at
    }
```

## 9. SSE events sent to the UI

| Event | Payload | UI behaviour |
| --- | --- | --- |
| `delta` | `{text}` | Appends streamed text to Milo's reply as it is written |
| `message` | `{text}` | Final full reply text (replaces the streamed text) |
| `tool_call` | `{name}` | Adds a progress step, e.g. "Saving campaign draft" |
| `tool_result` | `{name, result}` | Marks the step done; after `create_campaign_draft` the UI loads the draft and shows a proposal card; after `research_topic` it lists the sources with dates |
| `approval` | `{approval_id, summary, action, source, reason}` | Renders an Approve / Reject card |
| `error` | `{message}` | Error box in the reply (e.g. LLM rate limit) |
| `done` | `{}` | End of turn; UI refreshes approvals and campaigns |

The model's private reasoning (`thought` parts) is never sent. Each turn logs `chat turn: first response Xs, total Ys`.

## 10. Security and safety

- Meta token, app secret and OpenRouter key live only in `.env` on the server; the browser never sees them
  (`/api/status` reports only whether they are set).
- The UI is served from the backend's own origin; CORS is limited to `localhost` dev servers.
- The MCP server is request-only: no MCP tool can change a live ad, so a misbehaving model or an external MCP
  client can at most create an approval request.
- `actions.py` is the only code path to Meta write endpoints, and it runs only from `POST /api/approvals/{id}/approve`.
  The UI's request buttons (`POST /api/campaigns/{id}/requests`) create approvals exactly like the agent's tools.
- The tool guard blocks any tool not on the allow-list, so new server tools need a code review before the agent
  can use them.
- `META_DRY_RUN=true` by default, so a fresh checkout cannot touch Meta.
- Every draft, change request, approval, execution, rejection, failure, optimizer run and web research call is written to `AuditLog`.
- The optimizer has no path to Meta: it can only file approvals, exactly like the agent and the UI buttons.
- Web research results are **untrusted input**. `research_topic` and `read_landing_page` wrap them with a note that
  they are information only, the prompt tells Milo never to follow instructions found in them, text is stripped of
  tags and trimmed, only http(s) URLs are kept (landing pages: https only), and the UI opens links with
  `rel="noopener noreferrer nofollow"`. Because research tools are read-only and every change still needs the
  Approve click, a malicious page cannot publish, pause or change a budget.
- Only the search query (business + city) is sent to Tavily; no campaign IDs, budgets or keys. `TAVILY_API_KEY`
  stays in `.env`; `/api/status` reports only whether it is set.
- Draft validation enforces Meta rules before anything is sent (special ad categories must target ages 18–65).
