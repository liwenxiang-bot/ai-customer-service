"""Clean stray web channels + enforce globally-unique web channel keys.

The old public-chat path auto-created a web channel for ANY channel_key it received, so an
unknown / suspended tenant's key could spawn a stray channel under the default tenant (key !=
that tenant's slug). Such strays shadow the real tenant in tenant_for_channel (two rows share a
key → LIMIT 1 picks arbitrarily). A web channel's key must equal its tenant's slug; remove
anything else and add a partial unique index so the key (which the widget uses to resolve the
tenant) is globally unique from here on.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A web channel's key must equal its tenant's slug; anything else is an auto-created stray.
    op.execute(
        """
        DELETE FROM channel_configs cc USING tenants t
        WHERE cc.tenant_id = t.id AND cc.channel_type = 'web' AND cc.key <> t.slug
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_channel_key
        ON channel_configs (key) WHERE channel_type = 'web'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_web_channel_key")
