"""Performance analysis that keeps facts, calculations and assumptions apart.

- Facts: totals read from stored Meta Insights (or simulated) rows, with their dates and source. Nothing estimated.
- Calculations: every derived number (CTR, CPC, cost per result, change vs the previous period) with its formula
  and the inputs it used, so anyone can check it.
- Assumptions and caveats: what the reader must keep in mind (simulated data, small samples, attribution, a
  period with missing days, today's data still coming in).

Periods: the last `days` days up to the newest stored day, compared with the `days` before that
("this week vs last week"). Creatives: campaigns compared on CTR and cost per result, with a sample-size check.
Read-only: it never calls Meta and never changes anything.
"""
from datetime import date, timedelta

from app.config import get_settings
from app.db import store
from app.db.models import Campaign, MetricSnapshot

COUNTS = ["spend", "impressions", "reach", "clicks", "link_clicks", "landing_page_views", "conversations", "leads",
          "post_engagement"]
RESULT = {"website": ("landing_page_views", "landing page view"), "messenger": ("conversations", "conversation")}


def _totals(rows: list[MetricSnapshot]) -> dict:
    t = {k: 0 for k in COUNTS}
    for r in rows:
        for k in COUNTS:
            t[k] += getattr(r, k) or 0
    t["spend"] = round(t["spend"], 2)
    t["days_with_data"] = len({r.date for r in rows})
    return t


def _ratio(a: float, b: float, scale: float = 1, digits: int = 2) -> float | None:
    return round(a / b * scale, digits) if b else None


def _change(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev in (None, 0):
        return None
    return round((cur - prev) / prev * 100, 1)


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if unit == "inr":
        return f"₹{v:,.2f}"
    if unit == "%":
        return f"{v:,.2f}%"
    return f"{v:,}"


def _metrics(t: dict, result_field: str | None) -> dict:
    results = t.get(result_field, 0) if result_field else t["landing_page_views"] + t["conversations"]
    return {"ctr": _ratio(t["clicks"], t["impressions"], 100), "cpc": _ratio(t["spend"], t["clicks"]),
            "results": results, "cost_per_result": _ratio(t["spend"], results)}


def _calcs(label: str, cur_t: dict, prev_t: dict, cur_m: dict, prev_m: dict, result_name: str) -> list[dict]:
    out = [
        {"name": f"{label}: CTR", "formula": "clicks / impressions x 100",
         "inputs": f"{cur_t['clicks']:,} / {cur_t['impressions']:,}", "value": _fmt(cur_m["ctr"], "%")},
        {"name": f"{label}: CPC", "formula": "spend / clicks",
         "inputs": f"₹{cur_t['spend']:,.2f} / {cur_t['clicks']:,}", "value": _fmt(cur_m["cpc"], "inr")},
        {"name": f"{label}: cost per {result_name}", "formula": f"spend / {result_name}s",
         "inputs": f"₹{cur_t['spend']:,.2f} / {cur_m['results']:,}", "value": _fmt(cur_m["cost_per_result"], "inr")},
    ]
    for key, name, unit in (("spend", "spend", "inr"), ("clicks", "clicks", "")):
        ch = _change(cur_t[key], prev_t[key])
        out.append({"name": f"{label}: {name} change", "formula": "(this period - previous) / previous x 100",
                    "inputs": f"{_fmt(cur_t[key], unit)} vs {_fmt(prev_t[key], unit)}",
                    "value": "n/a (no previous data)" if ch is None else f"{ch:+.1f}%"})
    for key, name, unit in (("ctr", "CTR", "%"), ("cpc", "CPC", "inr"), ("cost_per_result", f"cost per {result_name}",
                                                                        "inr")):
        ch = _change(cur_m[key], prev_m[key])
        out.append({"name": f"{label}: {name} change", "formula": "(this period - previous) / previous x 100",
                    "inputs": f"{_fmt(cur_m[key], unit)} vs {_fmt(prev_m[key], unit)}",
                    "value": "n/a (no previous data)" if ch is None else f"{ch:+.1f}%"})
    return out


def analyze(campaign_id: int | None = None, days: int = 7) -> dict:
    days = max(1, min(int(days), 30))
    s = get_settings()
    if campaign_id is not None:
        c = store.get(Campaign, campaign_id)
        if c is None:
            raise ValueError(f"Campaign {campaign_id} not found.")
        campaigns = [c]
    else:
        campaigns = store.find(Campaign, sort=[("_id", 1)])
    ids = [c.id for c in campaigns]
    earliest = (date.today() - timedelta(days=2 * days + 2)).isoformat()
    rows = store.find(MetricSnapshot, {"campaign_id": {"$in": ids}, "date": {"$gte": earliest}}) if ids else []
    if not rows:
        return {"days": days, "has_data": False, "facts": [], "calculations": [], "assumptions": [
            "No metrics are stored yet for this selection: campaigns need to be live (or active in dry run) and "
            "the metrics pull to have run."], "campaigns": [], "creatives": [], "findings": []}
    end = date.fromisoformat(max(r.date for r in rows))
    cur_start, prev_end = end - timedelta(days=days - 1), end - timedelta(days=days)
    prev_start = prev_end - timedelta(days=days - 1)
    period = {"current": [cur_start.isoformat(), end.isoformat()],
              "previous": [prev_start.isoformat(), prev_end.isoformat()]}
    cur_rows = [r for r in rows if cur_start.isoformat() <= r.date <= end.isoformat()]
    prev_rows = [r for r in rows if prev_start.isoformat() <= r.date <= prev_end.isoformat()]
    sources = sorted({r.source for r in rows})
    by_id = {c.id: c for c in campaigns}

    per, calcs, creatives = [], [], []
    for cid in sorted({r.campaign_id for r in rows}):
        c = by_id[cid]
        rf, rname = RESULT.get(c.destination, RESULT["website"])
        ct = _totals([r for r in cur_rows if r.campaign_id == cid])
        pt = _totals([r for r in prev_rows if r.campaign_id == cid])
        cm, pm = _metrics(ct, rf), _metrics(pt, rf)
        per.append({"campaign_id": cid, "name": c.name, "status": c.status, "result": rname, "current": ct,
                    "previous": pt, "metrics": cm, "previous_metrics": pm,
                    "change_pct": {k: _change(cm[k], pm[k]) for k in ("ctr", "cpc", "cost_per_result")} |
                                  {k: _change(ct[k], pt[k]) for k in ("spend", "clicks")}})
        calcs += _calcs(c.name, ct, pt, cm, pm, rname)
        if ct["impressions"]:
            creatives.append({"campaign_id": cid, "name": c.name, "headline": c.headline,
                              "primary_text": c.primary_text, "has_image": bool(c.image_path),
                              "destination": c.destination, "impressions": ct["impressions"], "ctr": cm["ctr"],
                              "cpc": cm["cpc"], "result": rname, "cost_per_result": cm["cost_per_result"],
                              "enough_data": ct["impressions"] >= s.optimizer_min_impressions})

    at, ap = _totals(cur_rows), _totals(prev_rows)
    am, apm = _metrics(at, None), _metrics(ap, None)
    calcs = _calcs("Account", at, ap, am, apm, "result") + calcs
    creatives.sort(key=lambda x: (x["enough_data"], x["ctr"] or 0), reverse=True)

    facts = [
        {"what": "Period compared", "value": f"{period['current'][0]} to {period['current'][1]} vs "
                                             f"{period['previous'][0]} to {period['previous'][1]}"},
        {"what": "Data source", "value": ", ".join("Meta Insights" if x == "meta" else "simulated (dry run)"
                                                   for x in sources)},
        {"what": "Spend (this / previous period)", "value": f"₹{at['spend']:,.2f} / ₹{ap['spend']:,.2f}"},
        {"what": "Impressions", "value": f"{at['impressions']:,} / {ap['impressions']:,}"},
        {"what": "Clicks (all)", "value": f"{at['clicks']:,} / {ap['clicks']:,}"},
        {"what": "Link clicks", "value": f"{at['link_clicks']:,} / {ap['link_clicks']:,}"},
        {"what": "Landing page views", "value": f"{at['landing_page_views']:,} / {ap['landing_page_views']:,}"},
        {"what": "Messenger conversations started", "value": f"{at['conversations']:,} / {ap['conversations']:,}"},
        {"what": "Leads", "value": f"{at['leads']:,} / {ap['leads']:,}"},
        {"what": "Post engagement", "value": f"{at['post_engagement']:,} / {ap['post_engagement']:,}"},
    ]

    assumptions = []
    if "simulated" in sources:
        assumptions.append("Numbers are simulated demo data (dry run), not real Meta results.")
    if "meta" in sources:
        assumptions.append("Conversions use Meta's default attribution (7-day click, 1-day view); Meta may still "
                           "revise the last 1-3 days.")
    if at["days_with_data"] < days:
        assumptions.append(f"This period has data for {at['days_with_data']} of {days} days (campaigns paused, "
                           "new, or not pulled yet); totals cover only those days.")
    if not ap["days_with_data"]:
        assumptions.append("There is no data for the previous period, so changes can't be calculated yet.")
    small = [x["name"] for x in creatives if not x["enough_data"]]
    if small:
        assumptions.append(f"Fewer than {s.optimizer_min_impressions:,} impressions this period for: "
                           f"{', '.join(small)}. Their CTR and costs are not reliable enough to compare.")
    assumptions.append("A 'result' is a landing page view for website ads and a started conversation for "
                       "Messenger ads.")

    findings = []
    ch = _change(am["ctr"], apm["ctr"])
    if ch is not None:
        findings.append(f"Account CTR is {_fmt(am['ctr'], '%')} vs {_fmt(apm['ctr'], '%')} the period before "
                        f"({ch:+.1f}%).")
    ch = _change(am["cost_per_result"], apm["cost_per_result"])
    if ch is not None:
        findings.append(f"Cost per result is {_fmt(am['cost_per_result'], 'inr')} vs "
                        f"{_fmt(apm['cost_per_result'], 'inr')} ({ch:+.1f}%; lower is better).")
    reliable = [x for x in creatives if x["enough_data"] and x["ctr"] is not None]
    if len(reliable) >= 2:
        best, worst = reliable[0], reliable[-1]
        findings.append(f"Best creative by CTR: \"{best['headline'] or best['name']}\" ({_fmt(best['ctr'], '%')}); "
                        f"weakest: \"{worst['headline'] or worst['name']}\" ({_fmt(worst['ctr'], '%')}).")
    elif creatives:
        findings.append("Not enough campaigns with enough impressions to compare creatives yet.")

    return {"days": days, "has_data": True, "period": period, "sources": sources, "facts": facts,
            "calculations": calcs, "assumptions": assumptions, "findings": findings,
            "account": {"current": at, "previous": ap, "metrics": am, "previous_metrics": apm,
                        "change_pct": {k: _change(am[k], apm[k]) for k in ("ctr", "cpc", "cost_per_result", "results")}
                        | {k: _change(at[k], ap[k]) for k in ("spend", "impressions", "clicks")}},
            "campaigns": per, "creatives": creatives}
