"""Tenant resolution — turn a request signal (channel key, slug) into a tenant id.

Backed by the SECURITY DEFINER functions from migration 0007 so the non-superuser app role
can resolve a tenant *before* any RLS context exists (the chicken-and-egg of "which tenant
owns this channel"). These return only an id, never tenant data.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def tenant_for_channel(db: AsyncSession, channel_type: str, key: str) -> uuid.UUID | None:
    row = await db.execute(
        text("SELECT tenant_for_channel(:t, :k)"), {"t": channel_type, "k": key}
    )
    return row.scalar()


async def tenant_for_slug(db: AsyncSession, slug: str) -> uuid.UUID | None:
    row = await db.execute(text("SELECT tenant_for_slug(:s)"), {"s": slug})
    return row.scalar()
