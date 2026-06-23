"""Knowledge base: items, chunks (vector + FTS unit), versions, review queue, rebuild jobs.

Per requirements §9: knowledge items and their chunks are SEPARATE — chunks are the
unit of embedding and full-text search. Embedding dimension is read from config
(not a hard-wired literal); the vector column carries an HNSW index. Switching the
embedding model triggers a full rebuild tracked by EmbeddingRebuildJob.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
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

from app.config import settings
from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk
from app.models.enums import ChunkStatus, KnowledgeSource, KnowledgeStatus, ReviewStatus


class KnowledgeItem(Base, TimestampMixin, TenantMixin):
    __tablename__ = "knowledge_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="", index=True)
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=KnowledgeStatus.PUBLISHED, index=True
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default=KnowledgeSource.MANUAL)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    extra: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="item", cascade="all, delete-orphan", lazy="selectin"
    )


class KnowledgeChunk(Base, TimestampMixin, TenantMixin):
    """A retrievable unit: embedding (vector) + full-text index live here.

    `tsv` (a generated tsvector) and a trigram index on `content` are created in the
    migration — they back the keyword side of hybrid search and exact-term matching
    (product models, error codes) that pure vector search misses.
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Dimension comes from config (EMBEDDING_DIM / active AIConfig), never hard-wired.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(settings.embedding_dim), nullable=True
    )
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ChunkStatus.PENDING, index=True
    )
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    item: Mapped[KnowledgeItem] = relationship(back_populates="chunks")


class KnowledgeVersion(Base, TimestampMixin, TenantMixin):
    """Immutable snapshot of an item for history / diff / rollback."""

    __tablename__ = "knowledge_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    editor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    editor_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class KnowledgeReviewCandidate(Base, TimestampMixin, TenantMixin):
    """Auto-distilled knowledge awaiting human approval (requirements §7 待审核列表)."""

    __tablename__ = "knowledge_review_candidates"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    raw_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    suggested_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggested_category: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReviewStatus.PENDING, index=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_item_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class EmbeddingRebuildJob(Base, TimestampMixin, TenantMixin):
    """Tracks a full vector rebuild triggered by an embedding-model/dim change.

    During a rebuild, retrieval degrades (pure LLM) and progress is surfaced in the
    admin UI (requirements §6 Embedding 迁移).
    """

    __tablename__ = "embedding_rebuild_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    from_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    to_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    from_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    to_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
