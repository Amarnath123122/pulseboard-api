"""Background workers.

- review_scheduler: finds overview boards whose next review is due and enqueues them.
- review_worker:    drains the queue and runs one review per message; poison messages are dropped after
                    PB_MAX_DELIVERY_ATTEMPTS.
Both keep running through queue outages with backoff.
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.assistants.reviewer import review_board
from app.core.config import get_settings
from app.db.models import Board, utcnow
from app.db.session import db
from app.services.queue import review_queue

log = logging.getLogger(__name__)


async def enqueue_review(board_id: str, user_id: str) -> str:
    body = {"boardId": board_id, "userId": user_id, "enqueuedAt": utcnow().isoformat()}
    return await asyncio.to_thread(lambda: review_queue().send(body))


async def review_scheduler(stop: asyncio.Event) -> None:
    interval = get_settings().review_scheduler_interval_seconds
    while not stop.is_set():
        try:
            async with db.session() as session:
                stmt = (
                    select(Board)
                    .where(Board.kind_cd == "OVERVIEW", Board.build_status_cd == "LIVE",
                           Board.next_review_at.is_not(None), Board.next_review_at <= utcnow())
                    .limit(100)
                    .with_for_update(skip_locked=True)  # several instances may run this loop
                )
                due = list((await session.execute(stmt)).scalars())
                for board in due:
                    await enqueue_review(board.id, board.user_id)
                    board.next_review_at = None  # set again when the review publishes
                await session.commit()
                if due:
                    log.info("reviews scheduled", extra={"ctx": {"count": len(due)}})
        except Exception:  # noqa: BLE001
            log.warning("review scheduler tick failed", exc_info=True)
        await _sleep(stop, interval)


async def review_worker(stop: asyncio.Event) -> None:
    settings = get_settings()
    backoff = 1.0
    while not stop.is_set():
        try:
            messages = await asyncio.to_thread(lambda: review_queue().receive(5, 5))
            backoff = 1.0
        except Exception:  # noqa: BLE001
            log.warning("queue receive failed (is the local queue emulator running?); retry in %.0fs", backoff)
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, 60)
            continue
        if not messages:
            await _sleep(stop, 2)
            continue
        for msg in messages:
            if msg.attempts > settings.max_delivery_attempts:
                log.error("dropping poison review message", extra={"ctx": {"body": msg.body}})
                await asyncio.to_thread(review_queue().delete, msg)
                continue
            try:
                enqueued = datetime.fromisoformat(msg.body["enqueuedAt"]) if msg.body.get("enqueuedAt") else None
                await review_board(msg.body["boardId"], msg.body["userId"], enqueued)
            except Exception:  # noqa: BLE001
                log.exception("review message failed; it will be redelivered")
                continue
            await asyncio.to_thread(review_queue().delete, msg)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
