"""Redis cache. PB_REDIS_URL points at a real Redis (Azure Managed Redis in the cloud); when it is unset the
app uses an in-process fake Redis with the same API, so local runs and tests need no server.
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
        url = get_settings().redis_url
        if url:
            _client = redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2,
                                     health_check_interval=30)
        else:
            import fakeredis

            _client = fakeredis.FakeAsyncRedis(decode_responses=True)
    return _client


async def get_json(key: str) -> Any | None:
    try:
        raw = await client().get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:  # noqa: BLE001
        log.warning("cache read failed", extra={"ctx": {"key": key}})
        return None


async def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        await client().set(key, json.dumps(value, default=str), ex=ttl or get_settings().cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        log.warning("cache write failed", extra={"ctx": {"key": key}})


async def delete_prefix(prefix: str) -> None:
    try:
        keys = [k async for k in client().scan_iter(match=f"{prefix}*", count=500)]
        if keys:
            await client().delete(*keys)
    except Exception:  # noqa: BLE001
        log.warning("cache invalidation failed", extra={"ctx": {"prefix": prefix}})


async def ping() -> bool:
    try:
        return bool(await client().ping())
    except Exception:  # noqa: BLE001
        return False


async def reset() -> None:
    """Tests: drop the client so the next call builds a fresh one."""
    global _client
    _client = None
