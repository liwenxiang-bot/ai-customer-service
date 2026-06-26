"""Super-admin flag for cross-tenant tenant management.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("is_super_admin", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Promote the existing default-tenant admin(s) so there is a super-admin out of the box.
    op.execute(
        "UPDATE admin_users SET is_super_admin = true "
        "WHERE tenant_id = '00000000-0000-0000-0000-000000000001' AND role = 'admin'"
    )


def downgrade() -> None:
    op.drop_column("admin_users", "is_super_admin")
