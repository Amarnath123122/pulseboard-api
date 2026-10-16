import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalise everything read back to aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Board(Timestamped, Base):
    """A page of tiles. OVERVIEW is the user's landing board; DETAIL and BRIEFING boards hang off it."""

    __tablename__ = "boards"
    __table_args__ = (UniqueConstraint("parent_board_id", "slot_key", name="uq_boards_parent_slot"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_board_id: Mapped[str | None] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    kind_cd: Mapped[str] = mapped_column(String(16))  # OVERVIEW | DETAIL | BRIEFING
    slot_key: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    origin_cd: Mapped[str] = mapped_column(String(24))  # SETUP | DEFAULT_TEMPLATE | REVIEW
    build_status_cd: Mapped[str] = mapped_column(String(16))  # BUILDING | LIVE | FAILED
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    version: Mapped[int] = mapped_column(Integer, default=0)
    claim_holder: Mapped[str | None] = mapped_column(String(128))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation: Mapped[int] = mapped_column(Integer, default=0)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class BoardVersion(Base):
    __tablename__ = "board_versions"
    __table_args__ = (UniqueConstraint("board_id", "version", name="uq_board_versions"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[str | None] = mapped_column(String(36))
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Tile(Timestamped, Base):
    __tablename__ = "tiles"
    __table_args__ = (UniqueConstraint("board_id", "position", name="uq_tiles_position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    slot_key: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    visual_cd: Mapped[str] = mapped_column(String(16))  # STAT | TREND | BREAKDOWN | NOTE
    tier_cd: Mapped[str] = mapped_column(String(16))  # ALERT | HEADLINE | CONTEXT
    priority_cd: Mapped[str] = mapped_column(String(16))  # LOW | MEDIUM | HIGH
    follow_up_cd: Mapped[str] = mapped_column(String(16))  # NONE | PENDING | SEEN | DONE
    detail_board_id: Mapped[str | None] = mapped_column(ForeignKey("boards.id", ondelete="SET NULL"))
    briefing_board_id: Mapped[str | None] = mapped_column(ForeignKey("boards.id", ondelete="SET NULL"))
    query_spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    presentation: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AssistantProfile(Timestamped, Base):
    """Registry of assistants: which model, instructions and tools serve a given key."""

    __tablename__ = "assistant_profiles"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    instructions: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[Any]] = mapped_column(default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AssistantJob(Base):
    __tablename__ = "assistant_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_type_cd: Mapped[str] = mapped_column(String(16))  # SETUP | REVIEW
    profile_key: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    board_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status_cd: Mapped[str] = mapped_column(String(16))  # RUNNING | SUCCEEDED | FAILED | SKIPPED
    error: Mapped[str | None] = mapped_column(Text)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Preference(Base):
    """What the assistant has learned about a user."""

    __tablename__ = "user_preferences"
    __table_args__ = (UniqueConstraint("user_id", "scope_cd", "key", name="uq_user_preferences_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    origin_cd: Mapped[str] = mapped_column(String(16))  # SETUP | REVIEW | USER
    scope_cd: Mapped[str] = mapped_column(String(16))  # LAYOUT | WATCHLIST
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Conversation(Timestamped, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    purpose_cd: Mapped[str] = mapped_column(String(16))  # SETUP
    status_cd: Mapped[str] = mapped_column(String(16))  # OPEN | CLOSED
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    author_cd: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
