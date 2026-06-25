"""handoff ticket priority

Adds handoff_tickets.priority (urgent/high/normal/low) to drive the agent workbench
queue ordering. Backward compatible: defaults to 'normal'. IF NOT EXISTS so a fresh DB
(where 0001's create_all already added the column from the model) is a no-op.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE handoff_tickets "
        "ADD COLUMN IF NOT EXISTS priority varchar(16) NOT NULL DEFAULT 'normal'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE handoff_tickets DROP COLUMN IF EXISTS priority")
