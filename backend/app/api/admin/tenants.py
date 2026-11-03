"""Super-admin tenant management: list / create (onboard) / suspend."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.db.session import get_db
from app.models.admin import AdminUser
from app.services import tenant as tsvc

router = APIRouter(prefix="/tenants", tags=["admin-tenants"])


@router.get("")
async def list_tenants(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_super_admin)
):
    return {"tenants": await tsvc.list_tenants(db)}


class CreateTenantIn(BaseModel):
    name: str
    slug: str = ""
    admin_email: str
    admin_password: str


@router.post("", status_code=201)
async def create_tenant(
    body: CreateTenantIn,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_super_admin),
):
    if len(body.admin_password) < 8:
        raise HTTPException(status_code=400, detail="管理员密码至少 8 位")
    # Tenant isolation is enforced ONLY by Postgres RLS. When the app connects as a role that
    # bypasses RLS (superuser / BYPASSRLS — the default single-tenant setup), a second tenant
    # would read and write everyone's data. Refuse instead of silently provisioning.
    if await rls_bypassed(db):
        raise HTTPException(
            status_code=409,
            detail=(
                "当前数据库连接角色可绕过行级安全（RLS），开通新租户会导致租户间数据互通。"
                "请先按部署手册设置 APP_DB_PASSWORD 并执行迁移启用 RLS，重启后再开通租户。"
            ),
        )
    if "@" not in body.admin_email:
        raise HTTPException(status_code=400, detail="管理员邮箱无效")
    try:
        return await tsvc.provision_tenant(
            body.name, body.slug, body.admin_email, body.admin_password
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class UpdateTenantIn(BaseModel):
    is_active: bool


@router.patch("/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: UpdateTenantIn,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_super_admin),
):
    ok = await tsvc.set_tenant_active(db, tenant_id, body.is_active)
    if not ok:
        raise HTTPException(status_code=404, detail="租户不存在")
    await db.commit()  # get_db doesn't auto-commit; without this the toggle is rolled back
    return {"ok": True}


async def rls_bypassed(db: AsyncSession) -> bool:
    """True when the app's DB role ignores RLS (superuser or BYPASSRLS)."""
    row = await db.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    )
    return bool(row.scalar())
