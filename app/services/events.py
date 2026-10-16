"""Realtime fan-out of board events to each user's open WebSockets.

With SQLite (one process) events are delivered in-process. With Postgres every instance LISTENs on a channel
and publish() goes through pg_notify, so an event raised on instance A reaches sockets held by instance B.
That keeps realtime working behind a load balancer with autoscale-out.
"""

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db.session import db

log = logging.getLogger(__name__)
CHANNEL = "pulseboard_events"


class EventHub:
    def __init__(self) -> None:
        self._sockets: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._relay = False

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].add(ws)

    async def disconnect(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets[user_id].discard(ws)
            if not self._sockets[user_id]:
                self._sockets.pop(user_id, None)

    async def publish(self, user_id: str, event: dict[str, Any]) -> None:
        if self._relay:
            try:
                async with db.engine.connect() as conn:  # type: ignore[union-attr]
                    payload = json.dumps({"user_id": user_id, "event": event}, default=str)
                    await conn.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})
                    await conn.commit()
                return
            except Exception:  # noqa: BLE001 - fall back to local delivery rather than drop the event
                log.warning("pg_notify failed; delivering locally", exc_info=True)
        await self._deliver(user_id, event)

    async def _deliver(self, user_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._sockets.get(user_id, ()))
        for ws in targets:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001 - a dead socket must not break the publisher
                await self.disconnect(user_id, ws)

    async def run_relay(self, stop: asyncio.Event) -> None:
        """LISTEN loop for Postgres deployments; reconnects with backoff."""
        import asyncpg

        url = make_url(db.url or "")
        backoff = 1.0
        while not stop.is_set():
            conn = None
            try:
                conn = await asyncpg.connect(
                    host=url.host, port=url.port or 5432, user=url.username, password=url.password,
                    database=url.database, ssl=url.query.get("ssl"),
                )

                def _on_notify(_c, _pid, _ch, payload: str) -> None:  # noqa: ANN001
                    msg = json.loads(payload)
                    asyncio.get_running_loop().create_task(self._deliver(msg["user_id"], msg["event"]))

                await conn.add_listener(CHANNEL, _on_notify)
                self._relay = True
                backoff = 1.0
                log.info("event relay listening")
                await stop.wait()
            except Exception:  # noqa: BLE001
                self._relay = False
                log.warning("event relay disconnected; retrying in %.0fs", backoff, exc_info=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)
            finally:
                if conn is not None:
                    await conn.close()
        self._relay = False


hub = EventHub()
