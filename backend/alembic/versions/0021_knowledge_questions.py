"""knowledge_items.questions / questions_auto — customer phrasings indexed as question chunks.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.db.migration_utils import add_column_if_missing

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "knowledge_items",
        sa.Column("questions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    add_column_if_missing(
        "knowledge_items",
        sa.Column("questions_auto", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("knowledge_items", "questions_auto")
    op.drop_column("knowledge_items", "questions")
