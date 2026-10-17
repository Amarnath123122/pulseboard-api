"""Browser-facing endpoints."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistants import setup
from app.core.errors import AppError, NotFound
from app.core.security import Principal, require
from app.db.models import Board, Conversation, Tile
from app.db.session import get_session
from app.schemas import AssistantMessageIn, BoardOut, FollowUpIn, TileDataOut, TileOut
from app.services import cache, keys, layout, metrics
from app.services import catalog as catalog_service

router = APIRouter(prefix="/api/v1")


@router.post("/assistant/messages", tags=["assistant"])
async def assistant_message(
    body: AssistantMessageIn,
    me: Principal = Depends(require("setup:run")),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """One setup-assistant turn, streamed as text/event-stream."""
    if body.conversation_id:  # fail with a real status code before the stream opens
        convo = await session.get(Conversation, body.conversation_id)
        if convo is None or convo.user_id != me.user_id:
            raise NotFound("Conversation not found")

    async def stream() -> AsyncIterator[str]:
        try:
            async for chunk in setup.converse(me.user_id, body.conversation_id, body.action, body.text):
                yield chunk
        except AppError as exc:
            yield setup.sse("error", {"code": exc.code, "message": exc.message})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            yield setup.sse("error", {"code": "INTERNAL_ERROR", "message": "The assistant hit an error"})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/boards/overview", response_model=BoardOut, tags=["boards"])
async def my_overview(me: Principal = Depends(require("boards:read")), session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    return _owned(await layout.find_overview(session, me.user_id), me)


@router.get("/boards/{board_id}", response_model=BoardOut, tags=["boards"])
async def get_board(
    board_id: str, me: Principal = Depends(require("boards:read")), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await _board_dict(session, board_id, me)


@router.get("/boards/{board_id}/tiles", response_model=list[TileOut], tags=["boards"])
async def board_tiles(
    board_id: str, me: Principal = Depends(require("boards:read")), session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    """Tiles in position order. Cache-aside under `tiles:<board>`; publish and follow-up changes invalidate it."""
    await _board_dict(session, board_id, me)  # ownership check (itself cached)
    key = keys.tiles(board_id)
    if (hit := await cache.get_json(key)) is not None:
        return hit
    rows = (await session.execute(select(Tile).where(Tile.board_id == board_id).order_by(Tile.position))).scalars()
    tiles = [TileOut.model_validate(t).model_dump(mode="json") for t in rows]
    await cache.set_json(key, tiles)
    return tiles


@router.get("/boards/{board_id}/tile-data", response_model=list[TileDataOut], tags=["boards"])
async def board_tile_data(
    board_id: str, me: Principal = Depends(require("boards:read")), session: AsyncSession = Depends(get_session)
) -> list[TileDataOut]:
    """Data for every tile on a board in one request instead of one HTTP call per tile:
    one SQL query for the tiles, one Redis MGET for cached results, one pipelined SET for the misses."""
    await _board_dict(session, board_id, me)
    stmt = select(Tile).where(Tile.board_id == board_id).order_by(Tile.position)
    return await _tile_results(session, list((await session.execute(stmt)).scalars()))


@router.get("/tiles/{tile_id}/data", response_model=TileDataOut, tags=["tiles"])
async def tile_data(
    tile_id: str, me: Principal = Depends(require("boards:read")), session: AsyncSession = Depends(get_session)
) -> TileDataOut:
    """Runs one tile's saved query. A source failure is reported in the body (status ERROR) so one broken
    source never blanks the whole board."""
    return (await _tile_results(session, [await _owned_tile(session, tile_id, me)]))[0]


@router.patch("/tiles/{tile_id}/follow-up", response_model=TileOut, tags=["tiles"])
async def set_follow_up(
    tile_id: str, body: FollowUpIn, me: Principal = Depends(require("boards:follow_up")),
    session: AsyncSession = Depends(get_session),
) -> Tile:
    tile = await _owned_tile(session, tile_id, me)
    tile.follow_up_cd = body.follow_up_cd
    await session.commit()
    await session.refresh(tile)
    await cache.delete(keys.tiles(tile.board_id))
    return tile


async def _tile_results(session: AsyncSession, tiles: list[Tile]) -> list[TileDataOut]:
    hour = f"{datetime.now(UTC):%Y%m%d%H}"
    cached = await cache.get_many([keys.tile_data(t.id, hour) for t in tiles])
    misses = [t for t, hit in zip(tiles, cached, strict=True) if hit is None]
    fresh: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if misses:
        catalog = await catalog_service.load_catalog(session)  # loaded once (and itself cached) for all misses
        for t in misses:
            try:
                fresh[t.id] = await asyncio.to_thread(metrics.run_query, catalog, t.query_spec)
            except metrics.MetricsError as exc:
                errors[t.id] = str(exc)
        await cache.set_many({keys.tile_data(tid, hour): r for tid, r in fresh.items()})
    out = []
    for t, hit in zip(tiles, cached, strict=True):
        if hit is not None:
            out.append(TileDataOut(tile_id=t.id, status="OK", presentation=t.presentation, result=hit, cached=True))
        elif t.id in fresh:
            out.append(TileDataOut(tile_id=t.id, status="OK", presentation=t.presentation, result=fresh[t.id]))
        else:
            out.append(TileDataOut(tile_id=t.id, status="ERROR", presentation=t.presentation, error=errors[t.id]))
    return out


async def _board_dict(session: AsyncSession, board_id: str, me: Principal) -> dict[str, Any]:
    """Board by id, cache-aside under `board:<id>`. Ownership is checked on every read, cached or not."""
    key = keys.board(board_id)
    board = await cache.get_json(key)
    if board is None:
        row = await session.get(Board, board_id)
        if row is None:
            raise NotFound("Board not found")
        board = BoardOut.model_validate(row).model_dump(mode="json")
        if row.build_status_cd != "BUILDING":  # never cache a transient state
            await cache.set_json(key, board)
    if board["user_id"] != me.user_id:  # 404 rather than 403: don't reveal other users' boards
        raise NotFound("Board not found")
    return board


def _owned(board: Board | None, me: Principal) -> Board:
    if board is None or board.user_id != me.user_id:
        raise NotFound("Board not found")
    return board


async def _owned_tile(session: AsyncSession, tile_id: str, me: Principal) -> Tile:
    """Tile and ownership in one joined query, instead of loading the tile and then its board."""
    stmt = select(Tile).join(Board, Board.id == Tile.board_id).where(Tile.id == tile_id, Board.user_id == me.user_id)
    tile = (await session.execute(stmt)).scalar_one_or_none()
    if tile is None:
        raise NotFound("Tile not found")
    return tile
