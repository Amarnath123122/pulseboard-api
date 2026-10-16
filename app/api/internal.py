"""Service-to-service endpoints used by assistants and operators. Require the X-Service-Key header and are
not published through the API gateway."""

import secrets

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistants.reviewer import review_board
from app.core.config import get_settings
from app.core.errors import NotFound, Unauthorized
from app.db.models import AssistantJob, AssistantProfile, Board
from app.db.session import get_session
from app.schemas import BoardOut, ClaimIn, ClaimOut, JobOut, PreferenceIn, PreferenceOut, ProfileOut, PublishIn
from app.services import layout
from app.services.preferences import load_preferences, save_preference
from app.services.workers import enqueue_review


async def service_auth(x_service_key: str = Header(...)) -> None:
    if not secrets.compare_digest(x_service_key, get_settings().service_key):
        raise Unauthorized("Invalid service key")


router = APIRouter(prefix="/internal/v1", tags=["internal"], dependencies=[Depends(service_auth)])


@router.get("/assistant-profiles/{key}", response_model=ProfileOut)
async def get_profile(key: str, session: AsyncSession = Depends(get_session)) -> AssistantProfile:
    profile = await session.get(AssistantProfile, key)
    if profile is None:
        raise NotFound(f"Assistant profile {key} not found")
    return profile


@router.post("/preferences", response_model=PreferenceOut, status_code=status.HTTP_201_CREATED)
async def put_preference(body: PreferenceIn, session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    pref = await save_preference(session, user_id=body.user_id, origin=body.origin, scope=body.scope,
                                 key=body.key, value=body.value)
    await session.commit()
    return pref


@router.get("/preferences", response_model=list[PreferenceOut])
async def get_preferences(
    user_id: str, scopes: list[str] = Query(["LAYOUT", "WATCHLIST"]), session: AsyncSession = Depends(get_session)
):  # noqa: ANN201
    return await load_preferences(session, user_id=user_id, scopes=scopes)


@router.post("/layouts/claim", response_model=ClaimOut)
async def claim_layout(body: ClaimIn, session: AsyncSession = Depends(get_session)) -> ClaimOut:
    held = await layout.claim(session, user_id=body.user_id, origin=body.origin, claim_holder=body.claim_holder,
                              board_id=body.board_id)
    return ClaimOut(board_id=held.board_id, claim_holder=held.claim_holder,
                    claim_expires_at=held.claim_expires_at, generation=held.generation)


@router.post("/layouts/{board_id}/publish", response_model=BoardOut)
async def publish_layout(board_id: str, body: PublishIn, session: AsyncSession = Depends(get_session)) -> Board:
    return await layout.publish(session, board_id=board_id, claim_holder=body.claim_holder,
                                generation=body.generation, plan=body.plan, job_id=body.job_id)


@router.post("/reviews/{board_id}", status_code=status.HTTP_202_ACCEPTED)
async def queue_review(board_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    board = await session.get(Board, board_id)
    if board is None:
        raise NotFound("Board not found")
    return {"queued": True, "message_id": await enqueue_review(board.id, board.user_id)}


@router.post("/reviews/{board_id}/run", response_model=JobOut)
async def run_review_now(board_id: str, session: AsyncSession = Depends(get_session)) -> AssistantJob:
    """Synchronous review that bypasses the queue (operations and tests)."""
    board = await session.get(Board, board_id)
    if board is None:
        raise NotFound("Board not found")
    return await review_board(board.id, board.user_id)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    user_id: str | None = None, limit: int = Query(50, le=500), session: AsyncSession = Depends(get_session)
) -> list[AssistantJob]:
    stmt = select(AssistantJob).order_by(AssistantJob.started_at.desc()).limit(limit)
    if user_id:
        stmt = stmt.where(AssistantJob.user_id == user_id)
    return list((await session.execute(stmt)).scalars())
