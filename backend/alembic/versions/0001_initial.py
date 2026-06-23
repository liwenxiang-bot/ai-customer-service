"""initial schema

Creates the pgvector + pg_trgm extensions, all tables (from model metadata), the
full-text (tsvector) generated column, trigram + HNSW indexes that back hybrid
retrieval, and seeds the implicit default tenant.

Revision ID: 0001
Revises:
Create Date: 2026-06-23
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.config import settings
from app.db.base import Base

import app.models  # noqa: F401  (populate metadata)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions must exist before tables using the vector type are created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    # ---- Full-text search: generated tsvector column + GIN index ----
    # 'simple' config tokenises on non-alphanumerics (great for codes / English /
    # product models). For higher-quality Chinese segmentation, install zhparser or
    # pg_jieba and swap the config here — the retrieval layer is agnostic to it.
    op.execute(
        """
        ALTER TABLE knowledge_chunks
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED
        """
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON knowledge_chunks USING GIN (tsv)")

    # Trigram index → exact-term / substring matching incl. Chinese keywords.
    op.execute(
        "CREATE INDEX ix_chunks_content_trgm ON knowledge_chunks "
        "USING GIN (content gin_trgm_ops)"
    )

    # ---- Approximate nearest neighbour (HNSW) on the embedding vectors ----
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_semcache_embedding_hnsw ON semantic_cache "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # ---- Seed the implicit default tenant ----
    op.execute(
        f"""
        INSERT INTO tenants (id, name, slug, is_active, created_at, updated_at)
        VALUES ('{settings.default_tenant_id}', 'Default', 'default', true, now(), now())
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_semcache_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_trgm")
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    Base.metadata.drop_all(bind=op.get_bind())
