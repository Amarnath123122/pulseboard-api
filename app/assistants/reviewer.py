"""Board reviewer: periodically re-evaluates a user's overview board.

Queue message -> load assistant profile -> record an assistant_jobs row -> claim the board -> load preferences
-> read the current board -> find/describe/query/project metrics -> publish -> board.published event.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistants.planner import build_plan, default_areas
from app.core.errors import Conflict, NotFound
from app.db.models import AssistantJob, AssistantProfile, Board, Tile, utcnow
from app.db.session import db
from app.services import catalog as catalog_service
from app.services import layout
from app.services.preferences import as_dict, load_preferences

log = logging.getLogger(__name__)
PROFILE_KEY = "BOARD_REVIEWER"


async def review_board(board_id: str, user_id: str, enqueued_at: datetime | None = None) -> AssistantJob:
    async with db.session() as session:
        profile = await session.get(AssistantProfile, PROFILE_KEY)
        if profile is None or not profile.enabled:
            raise NotFound(f"Assistant profile {PROFILE_KEY} is missing or disabled")

        job = AssistantJob(job_type_cd="REVIEW", profile_key=PROFILE_KEY, user_id=user_id, board_id=board_id,
                           status_cd="RUNNING", enqueued_at=enqueued_at)
        session.add(job)
        await session.commit()
        job_id = job.id

        try:
            held = await layout.claim(session, user_id=user_id, origin="REVIEW", claim_holder=f"review:{job_id}",
                                      board_id=board_id)
        except Conflict as exc:
            job.status_cd, job.error, job.finished_at = "SKIPPED", exc.message, utcnow()
            await session.commit()
            return job

        try:
            prefs = as_dict(await load_preferences(session, user_id=user_id, scopes=["LAYOUT", "WATCHLIST"]))
            current = await read_board(session, board_id)
            catalog = await catalog_service.load_catalog(session)
            plan = build_plan(
                catalog,
                team=prefs.get("team", {}).get("text", ""),
                areas=prefs.get("areas", {}).get("areas") or current.get("areas") or default_areas(catalog),
                region=prefs.get("region", {}).get("region") or current.get("region", "ALL"),
            )
            await layout.publish(session, board_id=board_id, claim_holder=held.claim_holder,
                                 generation=held.generation, plan=plan, job_id=job_id)
            status, error = "SUCCEEDED", None
        except Exception as exc:  # noqa: BLE001
            log.exception("review failed", extra={"ctx": {"board_id": board_id}})
            await layout.release(session, board_id=board_id, claim_holder=held.claim_holder,
                                 generation=held.generation)
            status, error = "FAILED", str(exc)[:2000]

        job = await session.get(AssistantJob, job_id)  # type: ignore[assignment]
        job.status_cd, job.error, job.finished_at = status, error, utcnow()
        await session.commit()
        return job


async def read_board(session: AsyncSession, board_id: str) -> dict[str, Any]:
    """The board family and its follow-up states, as the reviewer sees it."""
    root = await session.get(Board, board_id)
    if root is None:
        raise NotFound("Board not found")
    tiles = (await session.execute(select(Tile).where(Tile.board_id == root.id))).scalars()
    return {
        "areas": root.settings.get("areas"),
        "region": root.settings.get("region", "ALL"),
        "follow_ups": {t.slot_key: t.follow_up_cd for t in tiles},
    }
