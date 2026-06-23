"""Cost/usage accounting (powers the daily cost circuit breaker) and semantic cache.

The live counter for the circuit breaker lives in Redis (fast, atomic); this table is
the durable daily rollup for the dashboard and post-hoc analysis.
"""

from __future__ import annotations

import uuid
from datetime import date

from pgvector.sqlalchemy import Vector
from sqlalchemy import Date, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class UsageDaily(Base, TimestampMixin, TenantMixin):
    __tablename__ = "usage_daily"
    __table_args__ = (
        UniqueConstraint("tenant_id", "day", "channel_type", name="uq_usage_day_channel"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    conversations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    escalations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SemanticCacheEntry(Base, TimestampMixin, TenantMixin):
    """Optional semantic cache: high-frequency Q→A hits return without an LLM call."""

    __tablename__ = "semantic_cache"

    id: Mapped[uuid.UUID] = uuid_pk()
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim), nullable=True
    )
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
