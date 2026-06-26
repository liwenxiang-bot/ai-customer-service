"""Resolve a tenant from an admin's email at login, so the slug field is optional.

In a multi-tenant deploy a tenant admin would otherwise have to type their slug to be routed
to the right tenant. Most admin emails are unique across tenants, so we can look the tenant up
by email (SECURITY DEFINER, bypassing RLS) and only fall back to asking for a slug when the
SAME email exists in more than one tenant. Returns the tenant_id only when unambiguous.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op
from app.config import settings

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tenant_for_admin_email(p_email text)
        RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER AS $func$
            SELECT CASE WHEN count(DISTINCT a.tenant_id) = 1
                        THEN (array_agg(DISTINCT a.tenant_id))[1] END
            FROM admin_users a
            JOIN tenants t ON t.id = a.tenant_id AND t.is_active
            WHERE lower(a.email) = lower(p_email) AND a.is_active
        $func$;
        """
    )
    if settings.app_db_password:
        op.execute(
            f"GRANT EXECUTE ON FUNCTION tenant_for_admin_email(text) TO {settings.app_db_user}"
        )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tenant_for_admin_email(text)")
