"""Abuse / off-topic guard settings on ai_configs.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.migration_utils import add_column_if_missing

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing(
        "ai_configs", sa.Column("guard_enabled", sa.Boolean(), nullable=False, server_default="true")
    )
    add_column_if_missing(
        "ai_configs", sa.Column("business_scope", sa.Text(), nullable=False, server_default="")
    )
    add_column_if_missing(
        "ai_configs", sa.Column("off_topic_reply", sa.Text(), nullable=False, server_default="")
    )
    add_column_if_missing(
        "ai_configs", sa.Column("max_input_chars", sa.Integer(), nullable=False, server_default="1000")
    )


def downgrade() -> None:
    for col in ("max_input_chars", "off_topic_reply", "business_scope", "guard_enabled"):
        op.drop_column("ai_configs", col)
