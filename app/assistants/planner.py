"""Turns a user's watchlist into a LayoutPlan.

Deterministic stand-in for model reasoning: find metrics -> query -> grade -> lay out the overview with
alerts first, each alert linking to a detail board and a briefing board.
"""

from typing import Any

from app.schemas import LayoutPlan, PlannedBoard, PlannedTile
from app.services import metrics

DEFAULT_AREAS = ["sales", "customers", "operations"]

_AREA_WORDS = {
    "sales": ["sales", "bookings", "pipeline", "revenue", "deals"],
    "customers": ["customer", "churn", "nps", "retention", "success"],
    "operations": ["operations", "ops", "delivery", "fulfil", "support", "tickets"],
    "engineering": ["engineering", "availability", "uptime", "reliability", "deploy", "platform"],
    "finance": ["finance", "spend", "cost", "budget", "cloud"],
}


def parse_areas(text: str) -> list[str]:
    lowered = text.lower()
    found = [area for area, words in _AREA_WORDS.items() if any(w in lowered for w in words)]
    return found or DEFAULT_AREAS


def parse_region(text: str) -> str:
    upper = text.upper()
    return next((r for r in metrics.REGIONS if r in upper), "ALL")


def build_plan(*, team: str, areas: list[str], region: str) -> LayoutPlan:
    params = {"region": region}
    alerts: list[tuple[str, dict[str, Any], str]] = []
    steady: list[str] = []
    found = metrics.find_metrics(areas)
    for metric in found:
        stat = metrics.run_query({"metric": metric, "op": "stat", "params": params})
        priority = metrics.grade(stat)
        if priority == "LOW":
            steady.append(metric)
        else:
            alerts.append((metric, stat, priority))
    alerts.sort(key=lambda a: (a[2] != "HIGH", -abs(a[1]["change_pct"])))

    overview_tiles: list[PlannedTile] = []
    details: list[PlannedBoard] = []
    briefings: list[PlannedBoard] = []

    for metric, stat, priority in alerts:
        meta = metrics.describe_metric(metric)
        unit = {"unit": meta["unit"]}
        detail_slot, briefing_slot = f"detail:{metric}", f"briefing:{metric}"
        summary = _summary(meta, stat)
        overview_tiles.append(PlannedTile(
            slot_key=f"alert:{metric}", title=meta["title"], visual="STAT", tier="ALERT", priority=priority,
            query_spec=_q(metric, "stat", params), presentation=unit | {"summary": summary},
            detail_slot_key=detail_slot, briefing_slot_key=briefing_slot,
        ))
        details.append(PlannedBoard(
            slot_key=detail_slot, kind="DETAIL", title=f"{meta['title']}: detail",
            settings={"metric": metric, "priority": priority},
            tiles=[
                PlannedTile(slot_key="series", title="Last 12 weeks", visual="TREND",
                            query_spec=_q(metric, "series", params), presentation=unit),
                PlannedTile(slot_key="regions", title="By region", visual="BREAKDOWN",
                            query_spec=_q(metric, "by_region", params), presentation=unit),
                PlannedTile(slot_key="projection", title="Next 4 weeks (projected)", visual="TREND",
                            query_spec=_q(metric, "projection", params), presentation=unit),
            ],
        ))
        briefings.append(PlannedBoard(
            slot_key=briefing_slot, kind="BRIEFING", title=f"{meta['title']}: briefing",
            settings={"metric": metric, "priority": priority, "summary": summary, "owners": meta["owners"],
                      "sources": meta["sources"], "next_steps": _next_steps(meta, priority, team)},
            tiles=[
                PlannedTile(slot_key="now", title="Now vs baseline", visual="STAT", priority=priority,
                            query_spec=_q(metric, "stat", params), presentation=unit),
                PlannedTile(slot_key="outlook", title="Outlook", visual="TREND",
                            query_spec=_q(metric, "projection", params), presentation=unit),
            ],
        ))

    for metric in steady:
        meta = metrics.describe_metric(metric)
        overview_tiles.append(PlannedTile(slot_key=f"stat:{metric}", title=meta["title"], visual="STAT",
                                          tier="HEADLINE", query_spec=_q(metric, "stat", params),
                                          presentation={"unit": meta["unit"]}))
    for metric in found[:2]:
        meta = metrics.describe_metric(metric)
        overview_tiles.append(PlannedTile(slot_key=f"trend:{metric}", title=f"{meta['title']} trend", visual="TREND",
                                          tier="CONTEXT", query_spec=_q(metric, "series", params),
                                          presentation={"unit": meta["unit"]}))

    overview = PlannedBoard(
        slot_key="overview", kind="OVERVIEW", title="Overview",
        settings={"team": team, "areas": areas, "region": region, "alert_count": len(alerts)},
        tiles=overview_tiles,
    )
    return LayoutPlan(overview=overview, details=details, briefings=briefings)


def _q(metric: str, op: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"metric": metric, "op": op, "params": dict(params)}


def _summary(meta: dict[str, Any], stat: dict[str, Any]) -> str:
    direction = "up" if stat["change_pct"] >= 0 else "down"
    return f"{meta['title']} is {direction} {abs(stat['change_pct']):.1f}% against its recent average."


def _next_steps(meta: dict[str, Any], priority: str, team: str) -> list[str]:
    steps = [f"Check recent drivers with {meta['owners'][0]}.",
             f"Confirm the data in {meta['sources'][0]} is complete before acting."]
    if priority == "HIGH":
        steps.insert(0, f"Raise it at the next {team or 'team'} sync.")
    return steps
