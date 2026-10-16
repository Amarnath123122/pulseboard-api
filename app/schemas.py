from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BoardKind = Literal["OVERVIEW", "DETAIL", "BRIEFING"]
Visual = Literal["STAT", "TREND", "BREAKDOWN", "NOTE"]
Tier = Literal["ALERT", "HEADLINE", "CONTEXT"]
Priority = Literal["LOW", "MEDIUM", "HIGH"]
FollowUp = Literal["NONE", "PENDING", "SEEN", "DONE"]
Origin = Literal["SETUP", "DEFAULT_TEMPLATE", "REVIEW"]


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- reads ----------
class BoardOut(OrmModel):
    id: str
    user_id: str
    parent_board_id: str | None
    kind_cd: str
    slot_key: str
    title: str
    origin_cd: str
    build_status_cd: str
    settings: dict[str, Any]
    version: int
    next_review_at: datetime | None
    updated_at: datetime


class TileOut(OrmModel):
    id: str
    board_id: str
    position: int
    slot_key: str
    title: str
    visual_cd: str
    tier_cd: str
    priority_cd: str
    follow_up_cd: str
    detail_board_id: str | None
    briefing_board_id: str | None
    presentation: dict[str, Any]


class TileDataOut(BaseModel):
    tile_id: str
    status: Literal["OK", "ERROR"]
    presentation: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class FollowUpIn(BaseModel):
    follow_up_cd: FollowUp


# ---------- layout plans ----------
class PlannedTile(BaseModel):
    slot_key: str
    title: str
    visual: Visual
    tier: Tier = "CONTEXT"
    priority: Priority = "LOW"
    follow_up: FollowUp | None = None  # None = keep the user's existing follow-up for this slot
    query_spec: dict[str, Any] = Field(default_factory=dict)
    presentation: dict[str, Any] = Field(default_factory=dict)
    detail_slot_key: str | None = None
    briefing_slot_key: str | None = None


class PlannedBoard(BaseModel):
    slot_key: str
    kind: BoardKind
    title: str
    settings: dict[str, Any] = Field(default_factory=dict)
    tiles: list[PlannedTile] = Field(default_factory=list)


class PlannedFollowUp(BaseModel):
    tile_slot_key: str
    follow_up: FollowUp


class LayoutPlan(BaseModel):
    overview: PlannedBoard
    details: list[PlannedBoard] = Field(default_factory=list)
    briefings: list[PlannedBoard] = Field(default_factory=list)
    follow_ups: list[PlannedFollowUp] = Field(default_factory=list)


class ClaimIn(BaseModel):
    user_id: str
    origin: Origin
    claim_holder: str
    board_id: str | None = None


class ClaimOut(BaseModel):
    board_id: str
    claim_holder: str
    claim_expires_at: datetime
    generation: int


class PublishIn(BaseModel):
    claim_holder: str
    generation: int
    plan: LayoutPlan
    job_id: str | None = None


# ---------- preferences / assistants ----------
class PreferenceIn(BaseModel):
    user_id: str
    origin: Literal["SETUP", "REVIEW", "USER"]
    scope: Literal["LAYOUT", "WATCHLIST"]
    key: str
    value: dict[str, Any]


class PreferenceOut(OrmModel):
    user_id: str
    origin_cd: str
    scope_cd: str
    key: str
    value: dict[str, Any]


class ProfileOut(OrmModel):
    key: str
    name: str
    model: str
    instructions: str
    tools: list[Any]
    enabled: bool


class JobOut(OrmModel):
    id: str
    job_type_cd: str
    profile_key: str
    user_id: str
    board_id: str | None
    status_cd: str
    error: str | None
    enqueued_at: datetime | None
    started_at: datetime
    finished_at: datetime | None


# ---------- assistant chat / auth ----------
class AssistantMessageIn(BaseModel):
    conversation_id: str | None = None
    action: Literal["start", "reply", "skip"] = "reply"
    text: str = Field(default="", max_length=2000)


class TokenIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
