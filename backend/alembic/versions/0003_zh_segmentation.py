"""chinese segmentation for keyword retrieval

Adds knowledge_chunks.content_seg (a jieba-segmented copy of content) and rebuilds the
generated `tsv` column to index BOTH the segmented Chinese tokens and the raw content
(so codes / English still match exactly). Backward compatible: until content_seg is
backfilled, tsv still indexes raw content exactly as before — no regression.

After upgrading, activate Chinese FTS for EXISTING knowledge with either:
    python -m scripts.resegment_chunks        # cheap: re-segments, no re-embedding
    (or trigger an embedding rebuild from the admin UI)

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the old generated tsv (its expression can't be ALTERed in place).
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS tsv")

    # IF NOT EXISTS: on a fresh DB, 0001's create_all() already added content_seg
    # (it's in the model now); on an existing DB this adds it.
    op.execute(
        "ALTER TABLE knowledge_chunks "
        "ADD COLUMN IF NOT EXISTS content_seg text NOT NULL DEFAULT ''"
    )

    # New tsv: segmented Chinese tokens + raw content (latin words / codes).
    op.execute(
        """
        ALTER TABLE knowledge_chunks
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(content_seg, '') || ' ' || coalesce(content, ''))
        ) STORED
        """
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON knowledge_chunks USING GIN (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS content_seg")
    op.execute(
        """
        ALTER TABLE knowledge_chunks
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED
        """
    )
    op.execute("CREATE INDEX ix_chunks_tsv ON knowledge_chunks USING GIN (tsv)")
