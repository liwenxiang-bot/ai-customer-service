"""Authentication: login, refresh-token rotation, current-user resolution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_password,
)
from app.models.admin import AdminUser, RefreshToken


class AuthError(Exception):
    pass


async def authenticate(db: AsyncSession, email: str, password: str) -> AdminUser:
    user = (
        await db.execute(select(AdminUser).where(AdminUser.email == email.lower().strip()))
    ).scalar_one_or_none()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        raise AuthError("邮箱或密码错误")
    user.last_login_at = datetime.now(UTC)
    return user


async def issue_tokens(db: AsyncSession, user: AdminUser) -> dict:
    access = create_access_token(str(user.id), user.role, {"email": user.email, "name": user.name})
    refresh, jti = create_refresh_token(str(user.id))
    db.add(
        RefreshToken(
            jti=jti,
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days),
        )
    )
    await db.flush()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user": {"id": str(user.id), "email": user.email, "name": user.name, "role": user.role},
    }


async def rotate_refresh(db: AsyncSession, jti: str, user_id: str) -> dict:
    """Validate a refresh token by jti, revoke it, and issue a fresh pair (rotation)."""
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    ).scalar_one_or_none()
    if not row or row.revoked or row.expires_at < datetime.now(UTC):
        raise AuthError("refresh token 无效或已过期")
    user = await db.get(AdminUser, uuid.UUID(user_id))
    if not user or not user.is_active:
        raise AuthError("账号不可用")

    new = await issue_tokens(db, user)
    row.revoked = True
    # Link rotation chain (new jti is embedded in the freshly issued refresh token).
    await db.flush()
    return new


async def revoke_all(db: AsyncSession, user_id: str) -> None:
    rows = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == uuid.UUID(user_id), RefreshToken.revoked.is_(False)
            )
        )
    ).scalars().all()
    for r in rows:
        r.revoked = True
    await db.flush()
