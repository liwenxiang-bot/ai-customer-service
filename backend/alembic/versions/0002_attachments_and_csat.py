"""Add message attachments + customer satisfaction on sessions.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("attachments", JSONB, nullable=False, server_default="[]"),
    )
    op.add_column("sessions", sa.Column("satisfaction_rating", sa.Integer(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("satisfaction_note", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("sessions", "satisfaction_note")
    op.drop_column("sessions", "satisfaction_rating")
    op.drop_column("messages", "attachments")
