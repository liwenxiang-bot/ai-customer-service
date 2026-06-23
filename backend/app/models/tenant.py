"""Tenant table — present from day one so multi-tenancy is an additive change.

In this single-tenant build there is exactly one row (the implicit default tenant,
id = settings.default_tenant_id). Future work adds tenant resolution + RLS without a
schema rewrite (see requirements §1, §19).
"""

from __future__ import annotations

import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, uuid_pk


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Default")
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, default="default")
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
