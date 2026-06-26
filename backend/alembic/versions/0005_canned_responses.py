"""canned (quick-reply) responses

Adds the canned_responses table for operator quick-reply templates. Uses checkfirst so a
fresh DB (where 0001's create_all already created it from the model) is a no-op.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from app.models.conversation import CannedResponse

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    CannedResponse.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    CannedResponse.__table__.drop(op.get_bind(), checkfirst=True)
