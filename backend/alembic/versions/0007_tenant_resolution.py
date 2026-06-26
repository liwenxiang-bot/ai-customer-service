"""Tenant resolution helpers for the runtime app role.

The app role (acs_app) can't read tenant tables until a tenant context is set — but for the
chat path the tenant IS what we're trying to discover from the channel. So we expose a
SECURITY DEFINER function that runs as the owner (bypassing RLS) and returns ONLY the
tenant_id for a channel — never any tenant data. Same idea for resolving a tenant by slug at
admin login (tenants has no RLS, but we add an explicit, grantable resolver for symmetry).

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

from app.config import settings

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_for_channel(p_type text, p_key text)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER AS $func$
            SELECT cc.tenant_id FROM channel_configs cc
            JOIN tenants t ON t.id = cc.tenant_id AND t.is_active
            WHERE cc.channel_type = p_type AND cc.key = p_key AND cc.enabled
            LIMIT 1
        $func$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_for_slug(p_slug text)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER AS $func$
            SELECT id FROM tenants WHERE slug = p_slug AND is_active LIMIT 1
        $func$;
        """
    )
    if settings.app_db_password:
        u = settings.app_db_user
        op.execute(f"GRANT EXECUTE ON FUNCTION tenant_for_channel(text, text) TO {u}")
        op.execute(f"GRANT EXECUTE ON FUNCTION tenant_for_slug(text) TO {u}")
        # The tenants registry has no RLS; the app role still needs to read/write it.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON tenants TO {u}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tenant_for_channel(text, text)")
    op.execute("DROP FUNCTION IF EXISTS tenant_for_slug(text)")
