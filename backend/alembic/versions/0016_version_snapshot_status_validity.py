"""Snapshot status + validity window on knowledge_versions.

Version snapshots only captured title/content/category/tags, so a mis-set
expires_at (a promo item accidentally "revived" or expired early) left no trace in
history and could not be undone by rollback. All three columns are nullable:
`status` is NULL exactly on snapshots that predate this migration, which rollback
treats as "not recorded — keep the item's current status/window".

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_versions", sa.Column("status", sa.String(32), nullable=True))
    op.add_column(
        "knowledge_versions",
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_versions",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_versions", "expires_at")
    op.drop_column("knowledge_versions", "effective_from")
    op.drop_column("knowledge_versions", "status")
