from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services import cache

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "degraded", "database": "down"}, status_code=503)
    # Redis is a cache: if it is down the API still works, so report it without failing readiness.
    cache_state = {True: "up", False: "down", None: "off"}[await cache.ping()]
    return JSONResponse({"status": "ok", "database": "up", "cache": cache_state})
