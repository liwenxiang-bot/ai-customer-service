"""Lock down the SECURITY DEFINER helpers: fixed search_path, no EXECUTE for PUBLIC.

They run as the owner and bypass RLS, so (a) an unqualified name must not resolve through a
caller-controlled schema, and (b) only the application's own roles may call them.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import re

from alembic import op
from app.config import settings

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_FUNCS = [
    "tenant_for_channel(text, text)",
    "tenant_for_slug(text)",
    "tenant_stats()",
    "tenant_for_admin_email(text)",
]


def upgrade() -> None:
    for fn in _FUNCS:
        op.execute(f"ALTER FUNCTION {fn} SET search_path = public, pg_temp")
        op.execute(f"REVOKE EXECUTE ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO CURRENT_USER")
        # The non-owner runtime role used when RLS is enabled (see 0006), if provisioned.
        role = settings.app_db_user
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role or ""):
            op.execute(
                f"""
                DO $$ BEGIN
                  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    EXECUTE 'GRANT EXECUTE ON FUNCTION {fn} TO {role}';
                  END IF;
                END $$;
                """
            )


def downgrade() -> None:
    for fn in _FUNCS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO PUBLIC")
        op.execute(f"ALTER FUNCTION {fn} RESET search_path")
