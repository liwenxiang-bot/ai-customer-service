"""Add message attachments + customer satisfaction on sessions.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op
from app.db.migration_utils import add_column_if_missing

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_column_if_missing("messages",
        sa.Column("attachments", JSONB, nullable=False, server_default="[]"),
    )
    add_column_if_missing("sessions", sa.Column("satisfaction_rating", sa.Integer(), nullable=True))
    add_column_if_missing("sessions",
        sa.Column("satisfaction_note", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("sessions", "satisfaction_note")
    op.drop_column("sessions", "satisfaction_rating")
    op.drop_column("messages", "attachments")
