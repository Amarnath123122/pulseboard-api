"""Layout claim / publish.

`claim` reserves the user's overview board for one writer for a short time and increments its generation
number. `publish` is accepted only from the current claim holder presenting the current generation, so a slow
or abandoned job can never overwrite a newer layout. `publish` writes every board in the family (overview,
details, briefings) in one transaction, matches existing boards and tiles by slot key, and records one
board_versions row per board.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import Conflict, NotFound
from app.db.models import Board, BoardVersion, Tile, as_utc, new_id, utcnow
from app.schemas import LayoutPlan, PlannedBoard
from app.services.events import hub

log = logging.getLogger(__name__)
OVERVIEW_SLOT = "overview"


@dataclass(frozen=True)
class Claim:
    board_id: str
    user_id: str
    claim_holder: str
    claim_expires_at: datetime
    generation: int


async def find_overview(session: AsyncSession, user_id: str) -> Board | None:
    stmt = select(Board).where(Board.user_id == user_id, Board.kind_cd == "OVERVIEW", Board.parent_board_id.is_(None))
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim(
    session: AsyncSession, *, user_id: str, origin: str, claim_holder: str, board_id: str | None = None
) -> Claim:
    now = utcnow()
    if board_id:
        stmt = select(Board).where(Board.id == board_id).with_for_update()
        board = (await session.execute(stmt)).scalar_one_or_none()
        if board is None or board.user_id != user_id:
            raise NotFound("Board not found")
    else:
        board = await find_overview(session, user_id)

    if board is None:
        board = Board(
            id=new_id(), user_id=user_id, parent_board_id=None, kind_cd="OVERVIEW", slot_key=OVERVIEW_SLOT,
            title="Overview", origin_cd=origin, build_status_cd="BUILDING", settings={}, version=0, generation=0,
        )
        session.add(board)
    elif board.claim_holder and board.claim_holder != claim_holder and (as_utc(board.claim_expires_at) or now) > now:
        raise Conflict("Board is being laid out by another job",
                       details={"claim_holder": board.claim_holder, "claim_expires_at": str(board.claim_expires_at)})

    board.claim_holder = claim_holder
    board.claim_expires_at = now + timedelta(seconds=get_settings().claim_seconds)
    board.generation = (board.generation or 0) + 1
    board.build_status_cd = "BUILDING"
    await session.commit()

    await hub.publish(user_id, {"type": "board.building", "boardId": board.id})
    return Claim(board.id, user_id, claim_holder, board.claim_expires_at, board.generation)


async def publish(
    session: AsyncSession, *, board_id: str, claim_holder: str, generation: int, plan: LayoutPlan,
    job_id: str | None = None,
) -> Board:
    now = utcnow()
    root = (await session.execute(select(Board).where(Board.id == board_id).with_for_update())).scalar_one_or_none()
    if root is None:
        raise NotFound("Board not found")
    if root.claim_holder != claim_holder or root.generation != generation:
        raise Conflict("Claim is no longer held at this generation", details={"current_generation": root.generation})
    if (as_utc(root.claim_expires_at) or now) <= now:
        raise Conflict("Claim expired")

    children = {b.slot_key: b for b in (await session.execute(
        select(Board).where(Board.parent_board_id == root.id))).scalars()}
    family_ids = [root.id, *(b.id for b in children.values())]

    # Keep the user's follow-up state when the plan has no opinion on a tile.
    prior_follow_ups = {(t.board_id, t.slot_key): t.follow_up_cd for t in (await session.execute(
        select(Tile).where(Tile.board_id.in_(family_ids)))).scalars()}
    await session.execute(delete(Tile).where(Tile.board_id.in_(family_ids)))

    boards: dict[str, tuple[Board, PlannedBoard]] = {OVERVIEW_SLOT: (root, plan.overview)}
    _apply(root, plan.overview)
    for planned in [*plan.details, *plan.briefings]:
        board = children.pop(planned.slot_key, None)
        if board is None:
            board = Board(
                id=new_id(), user_id=root.user_id, parent_board_id=root.id, slot_key=planned.slot_key,
                kind_cd=planned.kind, title=planned.title, origin_cd=root.origin_cd, build_status_cd="LIVE",
                settings={}, version=0, generation=0,
            )
            session.add(board)
        _apply(board, planned)
        boards[planned.slot_key] = (board, planned)

    if children:  # boards the plan no longer includes
        await session.execute(delete(Board).where(Board.id.in_([b.id for b in children.values()])))
    await session.flush()

    ids_by_slot = {slot: board.id for slot, (board, _) in boards.items()}
    explicit = {f.tile_slot_key: f.follow_up for f in plan.follow_ups}
    for board, planned in boards.values():
        for position, pt in enumerate(planned.tiles, start=1):
            session.add(Tile(
                id=new_id(), board_id=board.id, position=position, slot_key=pt.slot_key, title=pt.title,
                visual_cd=pt.visual, tier_cd=pt.tier, priority_cd=pt.priority,
                follow_up_cd=explicit.get(pt.slot_key) or pt.follow_up
                or prior_follow_ups.get((board.id, pt.slot_key)) or "NONE",
                detail_board_id=ids_by_slot.get(pt.detail_slot_key or ""),
                briefing_board_id=ids_by_slot.get(pt.briefing_slot_key or ""),
                query_spec=pt.query_spec, presentation=pt.presentation,
            ))
        board.version = (board.version or 0) + 1
        board.build_status_cd = "LIVE"
        session.add(BoardVersion(board_id=board.id, version=board.version, job_id=job_id,
                                 snapshot=planned.model_dump(mode="json")))

    root.claim_holder = None
    root.claim_expires_at = None
    root.next_review_at = now + timedelta(minutes=get_settings().review_interval_minutes)
    await session.commit()
    await session.refresh(root)

    log.info("layout published", extra={"ctx": {"board_id": root.id, "version": root.version}})
    await hub.publish(root.user_id, {"type": "board.published", "boardId": root.id, "version": root.version})
    return root


async def release(session: AsyncSession, *, board_id: str, claim_holder: str, generation: int) -> None:
    """Drop a claim after a failed job; the previously published layout (if any) stays visible."""
    await session.rollback()
    root = await session.get(Board, board_id)
    if root is None or root.claim_holder != claim_holder or root.generation != generation:
        return
    root.claim_holder = None
    root.claim_expires_at = None
    root.build_status_cd = "LIVE" if root.version > 0 else "FAILED"
    await session.commit()
    await hub.publish(root.user_id, {"type": "board.published", "boardId": root.id, "version": root.version})


def _apply(board: Board, planned: PlannedBoard) -> None:
    board.title = planned.title
    board.kind_cd = planned.kind if board.parent_board_id else "OVERVIEW"
    board.settings = planned.settings
