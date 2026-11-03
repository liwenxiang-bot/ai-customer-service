"""Admin auth endpoints: login, token refresh, current user, logout."""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.logging import get_logger
from app.core.ratelimit import clear_login_failures, login_locked_for, record_login_failure
from app.core.security import decode_token
from app.db.session import get_db, session_scope
from app.db.tenant_context import set_current_tenant
from app.models.admin import AdminUser
from app.services.audit import write_audit
from app.services.auth import AuthError, authenticate, issue_tokens, revoke_all, rotate_refresh
from app.services.tenant import tenant_for_admin_email

router = APIRouter(prefix="/auth", tags=["admin-auth"])
log = get_logger("admin.auth")


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip = _ip(request)
    wait = await login_locked_for(body.email, ip)
    if wait:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {max(wait // 60, 1)} 分钟后再试",
            headers={"Retry-After": str(wait)},
        )
    # No explicit tenant slug? Route by email (only when it maps to exactly one tenant) so the
    # slug field stays optional. Resolve in a separate session, then pin before the auth query
    # (the per-transaction GUC reads the context at transaction start).
    if not request.headers.get("x-tenant-slug"):
        async with session_scope() as s:
            tid = await tenant_for_admin_email(s, body.email)
        if tid:
            set_current_tenant(tid)
    try:
        user = await authenticate(db, body.email, body.password)
    except AuthError as exc:
        await record_login_failure(body.email, ip)
        log.warning("auth.login_failed", email=body.email, ip=ip)
        raise HTTPException(status_code=401, detail=str(exc))
    await clear_login_failures(body.email)
    tokens = await issue_tokens(db, user)
    await write_audit(db, user, "auth.login", "admin_user", str(user.id), ip=ip)
    await db.commit()
    return tokens


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    try:
        payload = decode_token(body.refresh_token)
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="token 类型错误")
        if payload.get("tenant"):
            set_current_tenant(payload["tenant"])  # re-establish RLS context for the lookup
        tokens = await rotate_refresh(db, payload["jti"], payload["sub"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="refresh token 无效")
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    await db.commit()
    return tokens


@router.get("/me")
async def me(user: AdminUser = Depends(get_current_user)):
    # agent_console_url lets the admin link operators to the standalone 坐席工作台 at the
    # right address (independent subdomain or {app_base_url}/agent) — the admin runs on a
    # different origin and can't know it otherwise.
    from app.services.handoff import agent_console_base

    return {
        "id": str(user.id), "email": user.email, "name": user.name,
        "role": user.role, "is_super_admin": user.is_super_admin,
        "agent_console_url": agent_console_base(),
    }


@router.post("/logout")
async def logout(user: AdminUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await revoke_all(db, str(user.id))
    await db.commit()
    return {"ok": True}


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
