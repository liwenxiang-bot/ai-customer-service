"""Multi-tenant Row-Level Security.

Adds DB-enforced tenant isolation:
  * a tenant_isolation RLS policy (USING + WITH CHECK) on every tenant table, keyed off the
    per-transaction GUC `app.tenant_id` (set by app.db.session._pin_tenant);
  * FORCE ROW LEVEL SECURITY so even the table owner is subject;
  * admin_users.email uniqueness scoped per-tenant instead of global;
  * a non-superuser login role `acs_app` (created only when APP_DB_PASSWORD is set) that the
    runtime app connects as — a superuser/owner would bypass RLS, so the app must NOT be one.

Backwards-compatible: with no app role configured the app keeps connecting as the owner and
the policies are inert (superuser bypasses RLS) → single-tenant behaviour is unchanged.
Set APP_DB_PASSWORD + point the app at acs_app to switch isolation on.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from alembic import op
from app.config import settings

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "sessions", "messages", "handoff_tickets", "canned_responses",
    "knowledge_items", "knowledge_chunks", "knowledge_versions",
    "knowledge_review_candidates", "embedding_rebuild_jobs",
    "channel_configs", "ai_configs", "usage_daily", "semantic_cache",
    "admin_users", "audit_logs",
]

_PREDICATE = "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    # ---- admin_users: email unique per-tenant, not globally ----
    op.execute("DROP INDEX IF EXISTS ix_admin_users_email")
    op.execute("CREATE INDEX IF NOT EXISTS ix_admin_users_email ON admin_users (email)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_users_tenant_email "
        "ON admin_users (tenant_id, email)"
    )

    # ---- RLS policy on every tenant table ----
    for t in TENANT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
        )

    # ---- dedicated non-superuser runtime role (only when a password is provided) ----
    pw = (settings.app_db_password or "").replace("'", "''")
    if pw:
        user = settings.app_db_user
        op.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{user}') THEN
                CREATE ROLE {user} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
              ELSE
                ALTER ROLE {user} LOGIN PASSWORD '{pw}' NOSUPERUSER NOBYPASSRLS;
              END IF;
            END $$;
            """
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {user}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}")
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {user}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {user}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {user}"
        )


def downgrade() -> None:
    for t in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP INDEX IF EXISTS uq_admin_users_tenant_email")
    op.execute("DROP INDEX IF EXISTS ix_admin_users_email")
    op.execute("CREATE UNIQUE INDEX ix_admin_users_email ON admin_users (email)")
