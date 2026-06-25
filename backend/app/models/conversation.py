"""Conversations: sessions, messages (with tool calls + citations + feedback), handoff tickets."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk
from app.models.enums import (
    HandoffReason,
    HandoffStatus,
    SessionStatus,
)


class Session(Base, TimestampMixin, TenantMixin):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_key: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    # End-user identity (anonymous UUID from localStorage, or a real upstream id)
    end_user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    end_user_display: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SessionStatus.ACTIVE, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    # Rolling summary of older turns (long-context compression)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summarized_until_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # ip, ua, locale...

    # Customer-submitted satisfaction (set when the visitor ends the session)
    satisfaction_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1–5
    satisfaction_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    messages: Mapped[list[Message]] = relationship(
        back_populates="session", cascade="all, delete-orphan",
        order_by="Message.seq",
    )


class Message(Base, TimestampMixin, TenantMixin):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # monotonic within session
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Tool-call trace: list of {name, arguments, result, status, duration_ms, error}
    tool_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Retrieval citations: list of {item_id, chunk_id, title, score, snippet}
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Customer-uploaded attachments: list of {url, name, content_type, size, kind}
    attachments: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Observability
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # User feedback (👍/👎) — a knowledge-distillation signal
    feedback: Mapped[str | None] = mapped_column(String(8), nullable=True)
    feedback_note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    session: Mapped[Session] = relationship(back_populates="messages")


class HandoffTicket(Base, TimestampMixin, TenantMixin):
    """Lightweight handoff: notify + record + tell the customer. Structured so it can
    later grow into a full agent workbench (requirements §8, §19) without a rewrite.
    """

    __tablename__ = "handoff_tickets"

    id: Mapped[uuid.UUID] = uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    end_user_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(32), nullable=False, default=HandoffReason.MODEL_DECISION)
    reason_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conversation_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=HandoffStatus.OPEN, index=True
    )
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal", index=True)
    notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class CannedResponse(Base, TimestampMixin, TenantMixin):
    """Reusable quick-reply templates operators can insert in the workbench."""

    __tablename__ = "canned_responses"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
