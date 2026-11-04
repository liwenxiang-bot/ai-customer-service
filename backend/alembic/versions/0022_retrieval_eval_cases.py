"""retrieval_eval_cases — labelled customer questions for threshold calibration.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from alembic import op
from app.db.migration_utils import create_index_if_missing

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_PREDICATE = "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_eval_cases (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            query text NOT NULL,
            expected_item_id uuid NULL,
            expected_title varchar(500) NOT NULL DEFAULT '',
            source varchar(32) NOT NULL DEFAULT 'manual',
            created_by varchar(255) NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    create_index_if_missing("ix_retrieval_eval_cases_tenant_id", "retrieval_eval_cases", ["tenant_id"])
    op.execute("ALTER TABLE retrieval_eval_cases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE retrieval_eval_cases FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON retrieval_eval_cases")
    op.execute(
        f"CREATE POLICY tenant_isolation ON retrieval_eval_cases "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS retrieval_eval_cases")
