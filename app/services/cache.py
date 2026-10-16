"""Cache, selected by PB_CACHE_BACKEND: a shared Redis, an in-process fake Redis (single process only), or none.
Cache failures never fail a request: reads fall back to the database."""

import json
import logging
from typing import Any

import redis.asyncio as redis

from app.core.config import get_settings

log = logging.getLogger(__name__)
_client: redis.Redis | None = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        s = get_settings()
        if s.cache_backend == "redis":
            if not s.redis_url:
                raise RuntimeError("PB_CACHE_BACKEND=redis needs PB_REDIS_URL")
            _client = redis.from_url(s.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
                                     health_check_interval=30)
        else:
            import fakeredis

            _client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return _client


def enabled() -> bool:
    return get_settings().cache_backend != "none"


async def get_json(key: str) -> Any | None:
    if not enabled():
        return None
    try:
        raw = await client().get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:  # noqa: BLE001
        log.warning("cache read failed", extra={"ctx": {"key": key}})
        return None


async def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    if not enabled():
        return
    try:
        await client().set(key, json.dumps(value, default=str), ex=ttl or get_settings().cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        log.warning("cache write failed", extra={"ctx": {"key": key}})


async def delete_prefix(prefix: str) -> None:
    if not enabled():
        return
    try:
        keys = [k async for k in client().scan_iter(match=f"{prefix}*", count=500)]
        if keys:
            await client().delete(*keys)
    except Exception:  # noqa: BLE001
        log.warning("cache invalidation failed", extra={"ctx": {"prefix": prefix}})


async def ping() -> bool | None:
    """True/False for a reachable/unreachable cache, None when caching is off."""
    if not enabled():
        return None
    try:
        return bool(await client().ping())
    except Exception:  # noqa: BLE001
        return False


async def reset() -> None:
    """Tests: drop the client so the next call builds a fresh one."""
    global _client
    _client = None
