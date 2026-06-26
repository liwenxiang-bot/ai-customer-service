"""Aggregated per-tenant stats for the admin list (avoids N+1 RLS-context round-trips).

A SECURITY DEFINER function so a super-admin gets counts across all tenants in one query
without switching RLS context per tenant. Returns only counts — no tenant data.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op
from app.config import settings

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_stats()
        RETURNS TABLE(id uuid, name text, slug text, is_active boolean,
                      created_at timestamptz, admins bigint, knowledge_items bigint,
                      web_channel_key text)
        LANGUAGE sql STABLE SECURITY DEFINER AS $func$
            SELECT t.id, t.name, t.slug, t.is_active, t.created_at,
                   (SELECT count(*) FROM admin_users a WHERE a.tenant_id = t.id),
                   (SELECT count(*) FROM knowledge_items k WHERE k.tenant_id = t.id),
                   (SELECT cc.key FROM channel_configs cc
                    WHERE cc.tenant_id = t.id AND cc.channel_type = 'web' LIMIT 1)
            FROM tenants t
            ORDER BY t.created_at ASC
        $func$;
        """
    )
    if settings.app_db_password:
        op.execute(f"GRANT EXECUTE ON FUNCTION tenant_stats() TO {settings.app_db_user}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tenant_stats()")
