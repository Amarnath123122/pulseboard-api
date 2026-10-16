"""Metric catalog: CRUD, paginated search, and the cached lookup used by assistants and tile queries."""

import math
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.db.models import Metric
from app.db.seed_metrics import INITIAL_METRICS
from app.services import cache, keys
from app.services.metrics import Catalog

CATALOG_KEY = keys.CATALOG
LIST_PREFIX = keys.METRIC_PAGES_PREFIX
FIELDS = ("area", "title", "unit", "base", "volatility", "higher_is_better", "owners", "sources", "enabled")
SORTABLE = {"key": Metric.key, "title": Metric.title, "area": Metric.area, "updated_at": Metric.updated_at}


def _as_dict(m: Metric) -> dict[str, Any]:
    return {"key": m.key, **{f: getattr(m, f) for f in FIELDS}, "updated_at": m.updated_at}


async def load_catalog(session: AsyncSession) -> Catalog:
    """Enabled metrics keyed by metric key. Cached; invalidated on every write."""
    cached = await cache.get_json(CATALOG_KEY)
    if cached is not None:
        return cached
    rows = (await session.execute(select(Metric).where(Metric.enabled.is_(True)))).scalars()
    catalog = {m.key: _as_dict(m) for m in rows}
    await cache.set_json(CATALOG_KEY, catalog)
    return catalog


async def list_areas(session: AsyncSession) -> list[str]:
    return sorted({m["area"] for m in (await load_catalog(session)).values()})


async def search(
    session: AsyncSession, *, q: str | None, area: str | None, enabled: bool | None,
    page: int, size: int, sort: str, desc: bool,
) -> dict[str, Any]:
    cache_key = f"{LIST_PREFIX}{q or ''}|{area or ''}|{enabled}|{page}|{size}|{sort}|{desc}"
    cached = await cache.get_json(cache_key)
    if cached is not None:
        return cached | {"cached": True}

    stmt = select(Metric)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Metric.key).like(like), func.lower(Metric.title).like(like),
                              func.lower(Metric.area).like(like)))
    if area:
        stmt = stmt.where(Metric.area == area)
    if enabled is not None:
        stmt = stmt.where(Metric.enabled.is_(enabled))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    column = SORTABLE.get(sort, Metric.key)
    stmt = stmt.order_by(column.desc() if desc else column.asc(), Metric.key).offset((page - 1) * size).limit(size)
    items = [_as_dict(m) for m in (await session.execute(stmt)).scalars()]
    result = {"items": items, "total": total, "page": page, "size": size, "pages": max(1, math.ceil(total / size))}
    await cache.set_json(cache_key, result, ttl=60)
    return result | {"cached": False}


async def get(session: AsyncSession, key: str) -> Metric:
    metric = await session.get(Metric, key)
    if metric is None:
        raise NotFound(f"Metric '{key}' not found")
    return metric


async def create(session: AsyncSession, data: dict[str, Any]) -> Metric:
    metric = Metric(**data)
    session.add(metric)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict(f"Metric '{data['key']}' already exists") from exc
    await _invalidate()
    await session.refresh(metric)
    return metric


async def update(session: AsyncSession, key: str, data: dict[str, Any]) -> Metric:
    metric = await get(session, key)
    for field, value in data.items():
        setattr(metric, field, value)
    await session.commit()
    await _invalidate()
    await session.refresh(metric)
    return metric


async def delete(session: AsyncSession, key: str) -> None:
    await session.delete(await get(session, key))
    await session.commit()
    await _invalidate()


async def seed_if_empty(session: AsyncSession) -> None:
    if (await session.execute(select(func.count()).select_from(Metric))).scalar_one():
        return
    for key, meta in INITIAL_METRICS.items():
        session.add(Metric(key=key, **{f: meta[f] for f in FIELDS if f in meta}))
    try:
        await session.commit()
    except IntegrityError:  # another worker seeded first
        await session.rollback()


async def _invalidate() -> None:
    await cache.delete_prefix(CATALOG_KEY)
    await cache.delete_prefix(LIST_PREFIX)
    await cache.delete_prefix(keys.TILE_DATA_PREFIX)
