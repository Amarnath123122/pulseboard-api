"""Browser-facing endpoints."""

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistants import setup
from app.core.config import get_settings
from app.core.errors import AppError, Forbidden, NotFound
from app.core.security import Principal, current_principal, issue_token
from app.db.models import Board, Conversation, Tile
from app.db.session import get_session
from app.schemas import AssistantMessageIn, BoardOut, FollowUpIn, TileDataOut, TileOut, TokenIn, TokenOut
from app.services import layout, metrics

router = APIRouter(prefix="/api/v1")


@router.post("/auth/dev-token", response_model=TokenOut, tags=["auth"])
async def dev_token(body: TokenIn) -> TokenOut:
    """Demo sign-in. Disable with PB_ENABLE_DEV_TOKEN=false once an identity provider is wired in."""
    if not get_settings().enable_dev_token:
        raise Forbidden("Demo sign-in is disabled")
    return TokenOut(access_token=issue_token(body.user_id), user_id=body.user_id)


@router.post("/assistant/messages", tags=["assistant"])
async def assistant_message(
    body: AssistantMessageIn,
    me: Principal = Depends(current_principal),
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
async def my_overview(me: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    return _owned(await layout.find_overview(session, me.user_id), me)


@router.get("/boards/{board_id}", response_model=BoardOut, tags=["boards"])
async def get_board(
    board_id: str, me: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)
) -> Board:
    return _owned(await session.get(Board, board_id), me)


@router.get("/boards/{board_id}/tiles", response_model=list[TileOut], tags=["boards"])
async def board_tiles(
    board_id: str, me: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)
) -> list[Tile]:
    _owned(await session.get(Board, board_id), me)
    stmt = select(Tile).where(Tile.board_id == board_id).order_by(Tile.position)
    return list((await session.execute(stmt)).scalars())


@router.get("/tiles/{tile_id}/data", response_model=TileDataOut, tags=["tiles"])
async def tile_data(
    tile_id: str, me: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)
) -> TileDataOut:
    """Runs the tile's saved query. A source failure is reported in the body (status ERROR) so one broken
    source never blanks the whole board."""
    tile = await _owned_tile(session, tile_id, me)
    try:
        result = await asyncio.to_thread(metrics.run_query, tile.query_spec)
        return TileDataOut(tile_id=tile.id, status="OK", presentation=tile.presentation, result=result)
    except metrics.MetricsError as exc:
        return TileDataOut(tile_id=tile.id, status="ERROR", presentation=tile.presentation, error=str(exc))


@router.patch("/tiles/{tile_id}/follow-up", response_model=TileOut, tags=["tiles"])
async def set_follow_up(
    tile_id: str, body: FollowUpIn, me: Principal = Depends(current_principal),
    session: AsyncSession = Depends(get_session),
) -> Tile:
    tile = await _owned_tile(session, tile_id, me)
    tile.follow_up_cd = body.follow_up_cd
    await session.commit()
    await session.refresh(tile)
    return tile


def _owned(board: Board | None, me: Principal) -> Board:
    if board is None or board.user_id != me.user_id:  # 404 rather than 403: don't reveal other users' boards
        raise NotFound("Board not found")
    return board


async def _owned_tile(session: AsyncSession, tile_id: str, me: Principal) -> Tile:
    tile = await session.get(Tile, tile_id)
    if tile is None:
        raise NotFound("Tile not found")
    _owned(await session.get(Board, tile.board_id), me)
    return tile
