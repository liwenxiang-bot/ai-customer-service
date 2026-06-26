"""Shared API dependencies: current-user extraction + role-based access control."""

from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_token
from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.models.admin import AdminUser
from app.models.enums import AdminRole

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AdminUser:
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供凭证")
    try:
        payload = decode_token(creds.credentials)
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="token 类型错误")
        user_id = payload["sub"]
        home = payload.get("tenant")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except (jwt.PyJWTError, KeyError):
        raise HTTPException(status_code=401, detail="无效凭证")

    # Load the authenticated user from THEIR home tenant — the request's data context may be a
    # different tenant (a super-admin acting-as one), and the user lives only in their own.
    with tenant_scope(home or DEFAULT_TENANT_ID):
        async with session_scope() as auth_db:
            user = await auth_db.get(AdminUser, uuid.UUID(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="账号不存在或已停用")
    return user


# Role hierarchy for permission checks.
_RANK = {AdminRole.READONLY: 0, AdminRole.OPERATOR: 1, AdminRole.ADMIN: 2}


def require_role(minimum: AdminRole):
    """Dependency factory enforcing a minimum role."""

    async def _checker(user: AdminUser = Depends(get_current_user)) -> AdminUser:
        if _RANK.get(user.role, -1) < _RANK[minimum]:
            raise HTTPException(status_code=403, detail="权限不足")
        return user

    return _checker


# Convenience dependencies.
require_readonly = require_role(AdminRole.READONLY)   # any authenticated user
require_operator = require_role(AdminRole.OPERATOR)   # operator or admin
require_admin = require_role(AdminRole.ADMIN)         # admin only


async def require_super_admin(user: AdminUser = Depends(get_current_user)) -> AdminUser:
    """Cross-tenant operator — may create/manage tenants."""
    if not getattr(user, "is_super_admin", False):
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return user
