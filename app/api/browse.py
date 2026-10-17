"""Read-only browsing for the signed-in user: app metadata, their boards and their assistant jobs.
Everything the UI needs for menus and filters comes from here, so the frontend hardcodes nothing."""

import math
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import Principal, current_principal, require
from app.db.models import AssistantJob, Board, Tile
from app.db.session import get_session
from app.schemas import BoardOut, JobOut
from app.services import cache, keys
from app.services import catalog as catalog_service

router = APIRouter(prefix="/api/v1", tags=["browse"])


class Meta(BaseModel):
    regions: list[str]
    units: list[str]
    areas: list[str]
    page_sizes: list[int]
    board_kinds: list[str]
    job_types: list[str]
    job_statuses: list[str]
    follow_ups: list[str]


class BoardSummary(BoardOut):
    tile_count: int
    alert_count: int


class Paged(BaseModel):
    total: int
    page: int
    size: int
    pages: int


class BoardPage(Paged):
    items: list[BoardSummary]


class JobPage(Paged):
    items: list[JobOut]


@router.get("/meta", response_model=Meta)
async def meta(_: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> Meta:
    s = get_settings()
    return Meta(
        regions=s.regions, units=s.units, page_sizes=s.page_sizes,
        areas=await catalog_service.list_areas(session),
        board_kinds=["OVERVIEW", "DETAIL", "BRIEFING"],
        job_types=["SETUP", "REVIEW"],
        job_statuses=["RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"],
        follow_ups=["NONE", "PENDING", "SEEN", "DONE"],
    )


def _pages(total: int, size: int) -> int:
    return max(1, math.ceil(total / size))


@router.get("/boards", response_model=BoardPage)
async def my_boards(
    q: str | None = Query(None, max_length=100),
    kind: Literal["OVERVIEW", "DETAIL", "BRIEFING"] | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    me: Principal = Depends(require("boards:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Every board the user owns, newest first, with tile and alert counts. Cached per user until it changes."""
    key = f"{keys.user_boards_prefix(me.user_id)}{q or ''}|{kind or ''}|{page}|{size}"
    if (hit := await cache.get_json(key)) is not None:
        return hit
    stmt = select(Board).where(Board.user_id == me.user_id)
    if q:
        stmt = stmt.where(func.lower(Board.title).like(f"%{q.lower()}%"))
    if kind:
        stmt = stmt.where(Board.kind_cd == kind)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    boards = list((await session.execute(
        stmt.order_by(Board.kind_cd != "OVERVIEW", Board.updated_at.desc()).offset((page - 1) * size).limit(size)
    )).scalars())
    counts: dict[str, tuple[int, int]] = {}
    if boards:
        is_alert = case((Tile.tier_cd == "ALERT", 1), else_=0)
        rows = await session.execute(
            select(Tile.board_id, func.count(), func.sum(is_alert))
            .where(Tile.board_id.in_([b.id for b in boards])).group_by(Tile.board_id)
        )
        counts = {bid: (n, int(a or 0)) for bid, n, a in rows}
    items = [
        {**BoardOut.model_validate(b).model_dump(mode="json"),
         "tile_count": counts.get(b.id, (0, 0))[0], "alert_count": counts.get(b.id, (0, 0))[1]}
        for b in boards
    ]
    result = {"items": items, "total": total, "page": page, "size": size, "pages": _pages(total, size)}
    await cache.set_json(key, result, ttl=60)
    return result


@router.get("/jobs", response_model=JobPage)
async def my_jobs(
    job_type: Literal["SETUP", "REVIEW"] | None = None,
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "SKIPPED"] | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    me: Principal = Depends(require("jobs:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The user's assistant activity (setups and reviews), newest first. Not cached: it changes constantly."""
    stmt = select(AssistantJob).where(AssistantJob.user_id == me.user_id)
    if job_type:
        stmt = stmt.where(AssistantJob.job_type_cd == job_type)
    if status:
        stmt = stmt.where(AssistantJob.status_cd == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    items = list((await session.execute(
        stmt.order_by(AssistantJob.started_at.desc()).offset((page - 1) * size).limit(size))).scalars())
    return {"items": items, "total": total, "page": page, "size": size, "pages": _pages(total, size)}
