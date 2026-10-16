"""Metric catalog CRUD with search, filters, sorting and pagination."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.schemas import UtcDateTime
from app.services import catalog

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"], dependencies=[Depends(current_principal)])



class MetricBase(BaseModel):
    area: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=32)
    base: float = Field(gt=0, description="Typical level the mock source generates around")
    volatility: float = Field(gt=0, le=1)
    higher_is_better: bool = True
    owners: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)
    enabled: bool = True

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, v: str) -> str:
        if v not in get_settings().units:
            raise ValueError(f"unit must be one of {get_settings().units}")
        return v


class MetricIn(MetricBase):
    key: str = Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_]*\.[a-z0-9_.]+$")


class MetricOut(MetricIn):
    updated_at: UtcDateTime | None = None


class MetricPage(BaseModel):
    items: list[MetricOut]
    total: int
    page: int
    size: int
    pages: int
    cached: bool


@router.get("", response_model=MetricPage)
async def list_metrics(
    q: str | None = Query(None, max_length=100, description="Search key, title or area"),
    area: str | None = None,
    enabled: bool | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    sort: Literal["key", "title", "area", "updated_at"] = "key",
    desc: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await catalog.search(session, q=q, area=area, enabled=enabled, page=page, size=size, sort=sort, desc=desc)


@router.get("/areas", response_model=list[str])
async def areas(session: AsyncSession = Depends(get_session)) -> list[str]:
    return await catalog.list_areas(session)


@router.get("/{key}", response_model=MetricOut)
async def get_metric(key: str, session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    return await catalog.get(session, key)


@router.post("", response_model=MetricOut, status_code=status.HTTP_201_CREATED)
async def create_metric(body: MetricIn, session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    return await catalog.create(session, body.model_dump())


@router.put("/{key}", response_model=MetricOut)
async def update_metric(key: str, body: MetricBase, session: AsyncSession = Depends(get_session)):  # noqa: ANN201
    return await catalog.update(session, key, body.model_dump())


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric(
    key: str, _: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)
) -> Response:
    await catalog.delete(session, key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
