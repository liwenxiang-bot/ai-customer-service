"""Admin auth endpoints: login, token refresh, current user, logout."""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import decode_token
from app.db.session import get_db
from app.db.tenant_context import set_current_tenant
from app.models.admin import AdminUser
from app.services.audit import write_audit
from app.services.auth import AuthError, authenticate, issue_tokens, revoke_all, rotate_refresh

router = APIRouter(prefix="/auth", tags=["admin-auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await authenticate(db, body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    tokens = await issue_tokens(db, user)
    await write_audit(db, user, "auth.login", "admin_user", str(user.id), ip=_ip(request))
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
    return {
        "id": str(user.id), "email": user.email, "name": user.name,
        "role": user.role, "is_super_admin": user.is_super_admin,
    }


@router.post("/logout")
async def logout(user: AdminUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await revoke_all(db, str(user.id))
    await db.commit()
    return {"ok": True}


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
