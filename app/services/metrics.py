"""Mock metrics gateway.

Stands in for a warehouse/semantic layer: a catalog of metrics, `describe_metric` for owners and sources,
`run_query` for results and `project` for a simple forecast. Values are deterministic per
(metric, region, hour), so reads within an hour agree and a review an hour later sees fresh numbers.
"""

import hashlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

REGIONS = ["NA", "EMEA", "APAC", "LATAM"]

CATALOG: dict[str, dict[str, Any]] = {
    "sales.bookings": {
        "area": "sales", "title": "New bookings", "unit": "USD", "base": 840_000, "volatility": 0.07,
        "higher_is_better": True, "owners": ["Revenue Operations"], "sources": ["crm.deals", "warehouse.bookings"],
    },
    "sales.pipeline_coverage": {
        "area": "sales", "title": "Pipeline coverage", "unit": "x", "base": 3.2, "volatility": 0.06,
        "higher_is_better": True, "owners": ["Sales Strategy"], "sources": ["crm.pipeline", "warehouse.quota"],
    },
    "customers.churn_rate": {
        "area": "customers", "title": "Logo churn", "unit": "%", "base": 2.1, "volatility": 0.10,
        "higher_is_better": False, "owners": ["Customer Success"], "sources": ["billing.subscriptions"],
    },
    "customers.nps": {
        "area": "customers", "title": "Net promoter score", "unit": "pts", "base": 41.0, "volatility": 0.06,
        "higher_is_better": True, "owners": ["Customer Insights"], "sources": ["survey.responses"],
    },
    "operations.delivery_lead_time": {
        "area": "operations", "title": "Order-to-delivery time", "unit": "days", "base": 4.8, "volatility": 0.07,
        "higher_is_better": False, "owners": ["Fulfilment"], "sources": ["oms.orders", "carrier.events"],
    },
    "operations.support_backlog": {
        "area": "operations", "title": "Open support tickets", "unit": "tickets", "base": 320, "volatility": 0.12,
        "higher_is_better": False, "owners": ["Support"], "sources": ["helpdesk.tickets"],
    },
    "engineering.availability": {
        "area": "engineering", "title": "Service availability", "unit": "%", "base": 99.6, "volatility": 0.004,
        "higher_is_better": True, "owners": ["Site Reliability"], "sources": ["monitoring.probes"],
    },
    "engineering.change_failure_rate": {
        "area": "engineering", "title": "Change failure rate", "unit": "%", "base": 12.0, "volatility": 0.12,
        "higher_is_better": False, "owners": ["Platform Engineering"], "sources": ["ci.deployments", "incidents.log"],
    },
    "finance.cloud_spend": {
        "area": "finance", "title": "Cloud spend", "unit": "USD", "base": 210_000, "volatility": 0.05,
        "higher_is_better": False, "owners": ["FinOps"], "sources": ["cloud.billing_export"],
    },
}

AREAS = sorted({m["area"] for m in CATALOG.values()})
OPERATIONS = ("stat", "series", "by_region", "projection")
VOLUME_UNITS = {"USD", "tickets"}
COUNT_UNITS = {"tickets"}


class MetricsError(Exception):
    pass


def _rng(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:16], 16))


def find_metrics(areas: list[str]) -> list[str]:
    wanted = {a.lower() for a in areas}
    return [key for key, meta in CATALOG.items() if meta["area"] in wanted]


def describe_metric(metric: str) -> dict[str, Any]:
    meta = CATALOG.get(metric)
    if meta is None:
        raise MetricsError(f"Unknown metric '{metric}'")
    return {k: meta[k] for k in ("title", "unit", "owners", "sources", "higher_is_better")} | {"metric": metric}


def _values(metric: str, region: str, points: int, bucket: str) -> list[float]:
    meta = CATALOG[metric]
    rng = _rng(metric, region, bucket)
    if region == "ALL":
        level = meta["base"]
    elif meta["unit"] in VOLUME_UNITS:  # a region holds a share of a volume...
        level = meta["base"] * rng.uniform(0.2, 0.35)
    else:  # ...but a rate or score only differs a little between regions
        level = meta["base"] * rng.uniform(0.97, 1.03)
    if meta["unit"] == "%" and meta["higher_is_better"]:
        level = min(level, 100.0)
    vol = meta["volatility"]
    values = []
    for _ in range(points):
        level = max(0.0, level * (1 + rng.gauss(0, vol / 3)))
        values.append(level)
    if rng.random() < 0.45:  # occasional shock in the latest point so reviews have something to flag
        worse = -1 if meta["higher_is_better"] else 1
        values[-1] = max(0.0, values[-1] * (1 + worse * rng.uniform(4, 15) * vol))
    return [_tidy(metric, v) for v in values]


def _tidy(metric: str, value: float) -> float:
    """Counts are whole numbers; everything else keeps three decimals."""
    return float(int(round(value))) if CATALOG[metric]["unit"] in COUNT_UNITS else round(value, 3)


def run_query(spec: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    metric, op = spec.get("metric", ""), spec.get("op", "")
    params = spec.get("params", {}) or {}
    if metric not in CATALOG:
        raise MetricsError(f"Unknown metric '{metric}'")
    if op not in OPERATIONS:
        raise MetricsError(f"Unsupported operation '{op}'")
    if params.get("simulate_error"):
        raise MetricsError("Upstream source timed out")

    now = now or datetime.now(UTC)
    bucket = now.strftime("%Y%m%d%H")
    region = params.get("region", "ALL")
    points = int(params.get("points", 12))
    meta = CATALOG[metric]

    if op == "by_region":
        rows = [{"label": r, "value": _values(metric, r, points, bucket)[-1]} for r in REGIONS]
        return {"shape": "categories", "unit": meta["unit"], "rows": rows}

    values = _values(metric, region, points, bucket)
    labels = [(now - timedelta(weeks=points - 1 - i)).strftime("%Y-%m-%d") for i in range(points)]
    if op == "stat":
        latest, prior = values[-1], sum(values[:-1]) / max(1, len(values) - 1)
        change = (latest - prior) / prior if prior else 0.0
        return {"shape": "stat", "unit": meta["unit"], "value": latest, "baseline": round(prior, 3),
                "change_pct": round(change * 100, 2), "higher_is_better": meta["higher_is_better"]}
    if op == "series":
        return {"shape": "series", "unit": meta["unit"],
                "rows": [{"label": lab, "value": v} for lab, v in zip(labels, values, strict=True)]}
    return project(metric, values, labels, horizon=int(params.get("horizon", 4)))


def project(metric: str, values: list[float], labels: list[str], horizon: int = 4) -> dict[str, Any]:
    """Least-squares linear projection."""
    n = len(values)
    mx, my = (n - 1) / 2, sum(values) / n
    denom = sum((x - mx) ** 2 for x in range(n)) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(range(n), values, strict=True)) / denom
    intercept = my - slope * mx
    last = datetime.strptime(labels[-1], "%Y-%m-%d")
    actual = [{"label": lab, "value": v, "projected": False} for lab, v in zip(labels, values, strict=True)]
    ahead = [{"label": (last + timedelta(weeks=i + 1)).strftime("%Y-%m-%d"),
              "value": _tidy(metric, max(0.0, intercept + slope * (n + i))), "projected": True} for i in range(horizon)]
    return {"shape": "series", "unit": CATALOG[metric]["unit"], "rows": actual + ahead}


def grade(stat: dict[str, Any]) -> str:
    """Priority from the direction-adjusted change against baseline."""
    signed = stat["change_pct"] if stat["higher_is_better"] else -stat["change_pct"]
    if signed <= -8:
        return "HIGH"
    if signed <= -3:
        return "MEDIUM"
    return "LOW"
