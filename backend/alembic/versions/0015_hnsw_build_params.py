"""Rebuild the chunks HNSW index with explicit build parameters.

pgvector's defaults (m=16, ef_construction=64) cap graph quality once the KB grows
past a few thousand chunks — edges lost at build time cannot be recovered by raising
ef_search later. ef_construction=200 buys better recall for a one-time build cost.

DROP+CREATE rather than REINDEX: REINDEX rebuilds from the stored definition, which
would keep the old storage parameters. The rebuild takes an exclusive lock and blocks
vector search for its duration — fine at dev scale; in production run this migration
at low traffic (CREATE INDEX CONCURRENTLY is not an option inside alembic's
transaction). The semantic_cache index keeps defaults: a cache miss only costs one
extra LLM call, not knowledge recall.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
