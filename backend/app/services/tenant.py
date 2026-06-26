"""Tenant resolution — turn a request signal (channel key, slug) into a tenant id.

Backed by the SECURITY DEFINER functions from migration 0007 so the non-superuser app role
can resolve a tenant *before* any RLS context exists (the chicken-and-egg of "which tenant
owns this channel"). These return only an id, never tenant data.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.models.tenant import Tenant


async def tenant_for_channel(db: AsyncSession, channel_type: str, key: str) -> uuid.UUID | None:
    row = await db.execute(
        text("SELECT tenant_for_channel(:t, :k)"), {"t": channel_type, "k": key}
    )
    return row.scalar()


async def tenant_for_slug(db: AsyncSession, slug: str) -> uuid.UUID | None:
    row = await db.execute(text("SELECT tenant_for_slug(:s)"), {"s": slug})
    return row.scalar()


# ----------------------------------------------------------------- management
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower().strip()).strip("-") or "tenant"


async def list_tenants(db: AsyncSession) -> list[dict]:
    """The tenants registry has no RLS, so a super-admin sees them all regardless of context."""
    rows = (await db.execute(select(Tenant).order_by(Tenant.created_at.asc()))).scalars().all()
    out = []
    for t in rows:
        # per-tenant counts must be read under that tenant's context (RLS)
        with tenant_scope(t.id):
            async with session_scope() as tdb:
                admins = (await tdb.execute(text("SELECT count(*) FROM admin_users"))).scalar()
                kb = (await tdb.execute(text("SELECT count(*) FROM knowledge_items"))).scalar()
                ch = (await tdb.execute(
                    text("SELECT key FROM channel_configs WHERE channel_type='web' LIMIT 1")
                )).scalar()
        out.append({
            "id": str(t.id), "name": t.name, "slug": t.slug, "is_active": t.is_active,
            "admins": admins, "knowledge_items": kb, "web_channel_key": ch,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return out


async def provision_tenant(name: str, slug: str, admin_email: str, admin_password: str) -> dict:
    """Create a tenant + its default AI config, web channel, and first (tenant) admin.

    The tenant's web channel key == its slug (globally unique → resolvable by the widget)."""
    from app.core.security import hash_password
    from app.models.admin import AdminUser
    from app.models.enums import AdminRole
    from app.services.ai_config import get_active_ai_config
    from app.services.channel import get_web_channel

    slug = _slugify(slug or name)
    new_id = uuid.uuid4()

    with tenant_scope(DEFAULT_TENANT_ID):
        async with session_scope() as db:
            exists = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
            if exists:
                raise ValueError(f"slug 已存在：{slug}")
            db.add(Tenant(id=new_id, name=name, slug=slug, is_active=True))

    with tenant_scope(new_id):
        async with session_scope() as db:
            await get_active_ai_config(db)        # seed default AI config for the tenant
            await get_web_channel(db, slug)       # seed the tenant's web channel (key = slug)
            db.add(AdminUser(
                email=admin_email.lower().strip(), name="管理员",
                password_hash=hash_password(admin_password),
                role=AdminRole.ADMIN, is_active=True,
            ))

    return {"id": str(new_id), "name": name, "slug": slug, "web_channel_key": slug}


async def set_tenant_active(db: AsyncSession, tenant_id: str, active: bool) -> bool:
    t = await db.get(Tenant, uuid.UUID(tenant_id))
    if not t:
        return False
    t.is_active = active
    await db.flush()
    return True
