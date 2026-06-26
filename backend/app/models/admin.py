"""Admin accounts, refresh-token rotation, and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk
from app.models.enums import AdminRole


class AdminUser(Base, TimestampMixin, TenantMixin):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = uuid_pk()
    # Unique per-tenant (uq_admin_users_tenant_email), not globally — see migration 0006.
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=AdminRole.OPERATOR)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Cross-tenant operator who can manage tenants (orthogonal to the within-tenant role).
    is_super_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base, TimestampMixin):
    """One row per issued refresh token (rotation + revocation)."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = uuid_pk()
    jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base, TimestampMixin, TenantMixin):
    """Audit trail for sensitive operations (knowledge edits, config changes, logins)."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    target_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
