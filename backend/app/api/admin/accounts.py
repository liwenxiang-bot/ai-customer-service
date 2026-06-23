"""Account & permission admin: admin users CRUD + audit log viewer (admin only)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.security import hash_password
from app.db.session import get_db
from app.models.admin import AdminUser, AuditLog
from app.models.enums import AdminRole
from app.services.audit import write_audit

router = APIRouter(prefix="/accounts", tags=["admin-accounts"])


def _user_dict(u: AdminUser) -> dict:
    return {
        "id": str(u.id), "email": u.email, "name": u.name, "role": u.role,
        "is_active": u.is_active,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = (await db.execute(select(AdminUser).order_by(AdminUser.created_at.asc()))).scalars().all()
    return {"users": [_user_dict(u) for u in rows]}


class UserCreate(BaseModel):
    email: EmailStr
    name: str = ""
    password: str
    role: str = AdminRole.OPERATOR


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    if body.role not in (AdminRole.ADMIN, AdminRole.OPERATOR, AdminRole.READONLY):
        raise HTTPException(status_code=400, detail="非法角色")
    exists = (
        await db.execute(select(AdminUser).where(AdminUser.email == body.email.lower()))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="邮箱已存在")
    u = AdminUser(
        email=body.email.lower(), name=body.name, password_hash=hash_password(body.password), role=body.role
    )
    db.add(u)
    await db.flush()
    await write_audit(db, user, "account.create", "admin_user", str(u.id), {"role": body.role})
    await db.commit()
    return _user_dict(u)


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.put("/users/{user_id}")
async def update_user(
    user_id: str, body: UserUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)
):
    target = await db.get(AdminUser, uuid.UUID(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="账号不存在")
    data = body.model_dump(exclude_unset=True)
    if "password" in data and data["password"]:
        target.password_hash = hash_password(data.pop("password"))
    else:
        data.pop("password", None)
    for k, v in data.items():
        setattr(target, k, v)
    await write_audit(db, user, "account.update", "admin_user", str(target.id), data)
    await db.commit()
    return _user_dict(target)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    target = await db.get(AdminUser, uuid.UUID(user_id))
    if not target:
        raise HTTPException(status_code=404, detail="账号不存在")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    await db.delete(target)
    await write_audit(db, user, "account.delete", "admin_user", user_id, {})
    await db.commit()
    return {"ok": True}


@router.get("/audit-logs")
async def audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str = "",
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": str(a.id), "actor_email": a.actor_email, "action": a.action,
                "target_type": a.target_type, "target_id": a.target_id, "detail": a.detail,
                "ip": a.ip, "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ],
    }
