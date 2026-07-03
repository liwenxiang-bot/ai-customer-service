"""Promote live-takeover attribution from session.meta to dedicated columns.

start_takeover used to read-modify-write the whole session.meta JSONB to stash
takeover_by / takeover_by_name, which raced the concurrent client_msg_id bookkeeping
on the same column and lost updates. Move the attribution to real columns so the write
is a plain UPDATE that never touches meta; takeover_by_id also backs the queue-counts
"接待中（我的）" filter (indexed).

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("takeover_by_id", PG_UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("takeover_by_name", sa.String(length=120), nullable=True, server_default=""),
    )
    op.create_index("ix_sessions_takeover_by_id", "sessions", ["takeover_by_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_takeover_by_id", table_name="sessions")
    op.drop_column("sessions", "takeover_by_name")
    op.drop_column("sessions", "takeover_by_id")
