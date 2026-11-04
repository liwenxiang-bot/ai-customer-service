"""messages.answer_kind (what each assistant turn was) + knowledge_gap_ignores.

answer_kind drives the honest knowledge hit-rate (services.answer_kind); historical rows
are stamped by scripts/backfill_answer_kind.py. knowledge_gap_ignores holds customer
questions an operator dismissed from the "未命中问题" list.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.migration_utils import add_column_if_missing, create_index_if_missing

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_PREDICATE = "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    add_column_if_missing(
        "messages", sa.Column("answer_kind", sa.String(16), nullable=False, server_default="")
    )
    create_index_if_missing("ix_messages_answer_kind", "messages", ["answer_kind"])

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_gap_ignores (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            question text NOT NULL,
            created_by varchar(255) NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    create_index_if_missing("ix_knowledge_gap_ignores_tenant_id", "knowledge_gap_ignores", ["tenant_id"])
    # Same tenant isolation as every business table (see 0006).
    op.execute("ALTER TABLE knowledge_gap_ignores ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_gap_ignores FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON knowledge_gap_ignores")
    op.execute(
        f"CREATE POLICY tenant_isolation ON knowledge_gap_ignores "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_gap_ignores")
    op.execute("DROP INDEX IF EXISTS ix_messages_answer_kind")
    op.drop_column("messages", "answer_kind")
