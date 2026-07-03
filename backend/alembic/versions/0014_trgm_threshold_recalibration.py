"""Recalibrate stored trgm_threshold for the word_similarity() semantics change.

The trigram branch of keyword retrieval switched from similarity() to
word_similarity() (one-directional query-vs-content-window similarity). Values
calibrated for the old operator (default was 0.1) are meaningless under the new one:
word_similarity() scores sit an order of magnitude higher, so an old threshold lets
nearly everything through. Rows still carrying an old-semantics value (<= 0.2) are
bumped to the new default 0.4; anything above that is assumed to be a deliberate
post-change calibration and left alone.

Cross-tenant UPDATE on a FORCE-RLS table: migrations run as the owner/superuser DSN
(not the acs_app role), which bypasses the tenant policies — same pattern as 0008.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ai_configs
        SET retrieval = jsonb_set(retrieval, '{trgm_threshold}', '0.4'::jsonb)
        WHERE retrieval IS NOT NULL
          AND (retrieval->>'trgm_threshold') ~ '^[0-9.]+$'
          AND (retrieval->>'trgm_threshold')::float <= 0.2
        """
    )


def downgrade() -> None:
    # Intentional no-op: the old similarity()-calibrated values are unrecoverable, and
    # restoring them would be wrong anyway now that the query uses word_similarity().
    pass
