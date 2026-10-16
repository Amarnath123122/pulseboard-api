"""Mock metrics gateway.

Stands in for a warehouse/semantic layer. The catalog (one dict per metric key) is loaded from the
`metrics` table by app.services.catalog and passed in; `describe_metric` for owners and sources,
`run_query` for results and `project` for a simple forecast. Values are deterministic per
(metric, region, hour), so reads within an hour agree and a review an hour later sees fresh numbers.
"""

import hashlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings

Catalog = dict[str, dict[str, Any]]

OPERATIONS = ("stat", "series", "by_region", "projection")
VOLUME_UNITS = {"USD", "tickets"}
COUNT_UNITS = {"tickets"}


class MetricsError(Exception):
    pass


def _rng(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:16], 16))


def find_metrics(catalog: Catalog, areas: list[str]) -> list[str]:
    wanted = {a.lower() for a in areas}
    return [key for key, meta in catalog.items() if meta["area"] in wanted]


def describe_metric(catalog: Catalog, metric: str) -> dict[str, Any]:
    meta = catalog.get(metric)
    if meta is None:
        raise MetricsError(f"Unknown metric '{metric}'")
    return {k: meta[k] for k in ("title", "unit", "owners", "sources", "higher_is_better")} | {"metric": metric}


def _values(meta: dict[str, Any], metric: str, region: str, points: int, bucket: str) -> list[float]:
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
    return [_tidy(meta, v) for v in values]


def _tidy(meta: dict[str, Any], value: float) -> float:
    """Counts are whole numbers; everything else keeps three decimals."""
    return float(int(round(value))) if meta["unit"] in COUNT_UNITS else round(value, 3)


def run_query(catalog: Catalog, spec: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    metric, op = spec.get("metric", ""), spec.get("op", "")
    params = spec.get("params", {}) or {}
    if metric not in catalog:
        raise MetricsError(f"Unknown metric '{metric}'")
    if op not in OPERATIONS:
        raise MetricsError(f"Unsupported operation '{op}'")
    if params.get("simulate_error"):
        raise MetricsError("Upstream source timed out")

    now = now or datetime.now(UTC)
    bucket = now.strftime("%Y%m%d%H")
    region = params.get("region", "ALL")
    points = int(params.get("points", 12))
    meta = catalog[metric]

    if op == "by_region":
        rows = [{"label": r, "value": _values(meta, metric, r, points, bucket)[-1]} for r in get_settings().regions]
        return {"shape": "categories", "unit": meta["unit"], "rows": rows}

    values = _values(meta, metric, region, points, bucket)
    labels = [(now - timedelta(weeks=points - 1 - i)).strftime("%Y-%m-%d") for i in range(points)]
    if op == "stat":
        latest, prior = values[-1], sum(values[:-1]) / max(1, len(values) - 1)
        change = (latest - prior) / prior if prior else 0.0
        return {"shape": "stat", "unit": meta["unit"], "value": latest, "baseline": round(prior, 3),
                "change_pct": round(change * 100, 2), "higher_is_better": meta["higher_is_better"]}
    if op == "series":
        return {"shape": "series", "unit": meta["unit"],
                "rows": [{"label": lab, "value": v} for lab, v in zip(labels, values, strict=True)]}
    return project(meta, values, labels, horizon=int(params.get("horizon", 4)))


def project(meta: dict[str, Any], values: list[float], labels: list[str], horizon: int = 4) -> dict[str, Any]:
    """Least-squares linear projection."""
    n = len(values)
    mx, my = (n - 1) / 2, sum(values) / n
    denom = sum((x - mx) ** 2 for x in range(n)) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(range(n), values, strict=True)) / denom
    intercept = my - slope * mx
    last = datetime.strptime(labels[-1], "%Y-%m-%d")
    actual = [{"label": lab, "value": v, "projected": False} for lab, v in zip(labels, values, strict=True)]
    ahead = [{"label": (last + timedelta(weeks=i + 1)).strftime("%Y-%m-%d"),
              "value": _tidy(meta, max(0.0, intercept + slope * (n + i))), "projected": True} for i in range(horizon)]
    return {"shape": "series", "unit": meta["unit"], "rows": actual + ahead}


def grade(stat: dict[str, Any]) -> str:
    """Priority from the direction-adjusted change against baseline."""
    signed = stat["change_pct"] if stat["higher_is_better"] else -stat["change_pct"]
    if signed <= -8:
        return "HIGH"
    if signed <= -3:
        return "MEDIUM"
    return "LOW"
