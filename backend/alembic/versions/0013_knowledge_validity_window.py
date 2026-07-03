"""Knowledge validity window: effective_from / expires_at on knowledge_items.

Customer-service knowledge is often time-boxed (促销活动、旧资费): without a validity
window, an expired item keeps competing with current policy at full weight — and old
policy text is usually the closest lexical match to how users ask. NULL on either side
means unbounded; retrieval filters out items outside the window.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_items", "expires_at")
    op.drop_column("knowledge_items", "effective_from")
