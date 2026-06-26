"""Channel configuration admin: Web branding + anti-abuse, handoff notify, WeChat Work."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.config import settings
from app.core.encryption import encrypt_secret, mask_secret
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.config import ChannelConfig
from app.models.enums import ChannelType
from app.services.audit import write_audit
from app.services.channel import get_tenant_web_channel

router = APIRouter(prefix="/channels", tags=["admin-channels"])

_MASK = "••••••••"


async def _get_channel(db: AsyncSession, channel_type: str, key: str = "default") -> ChannelConfig:
    row = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == channel_type, ChannelConfig.key == key
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="渠道配置不存在")
    return row


@router.get("")
async def list_channels(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    rows = (await db.execute(select(ChannelConfig))).scalars().all()
    return {
        "channels": [
            {"id": str(c.id), "channel_type": c.channel_type, "key": c.key, "name": c.name, "enabled": c.enabled}
            for c in rows
        ]
    }


# ------------------------------------------------------------------- web
@router.get("/web")
async def get_web(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    c = await get_tenant_web_channel(db)
    await db.commit()
    return {
        "id": str(c.id), "key": c.key, "enabled": c.enabled, "settings": c.settings,
        "allowed_domains": c.allowed_domains,
        "rate_limit_user_per_min": c.rate_limit_user_per_min,
        "rate_limit_ip_per_min": c.rate_limit_ip_per_min,
        "system_prompt_override": c.system_prompt_override,
        # public base URL where the widget/chat are served (may differ from the admin origin)
        "app_base_url": settings.app_base_url.rstrip("/"),
    }


class WebUpdate(BaseModel):
    enabled: bool | None = None
    settings: dict | None = None
    allowed_domains: list[str] | None = None
    rate_limit_user_per_min: int | None = None
    rate_limit_ip_per_min: int | None = None
    system_prompt_override: str | None = None


@router.put("/web")
async def update_web(body: WebUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    c = await get_tenant_web_channel(db)
    data = body.model_dump(exclude_unset=True)
    if "settings" in data and data["settings"] is not None:
        c.settings = {**c.settings, **data.pop("settings")}
    for k, v in data.items():
        setattr(c, k, v)
    await write_audit(db, user, "channel.update", "channel", "web", {})
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------- notify
@router.get("/notify")
async def get_notify(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    c = await _get_channel(db, "notify")
    s = dict(c.settings)
    s["smtp_password_enc"] = mask_secret(s.get("smtp_password_enc"))
    return {"id": str(c.id), "enabled": c.enabled, "settings": s}


class NotifyUpdate(BaseModel):
    enabled: bool | None = None
    wechat_webhook_url: str | None = None
    email_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_ssl: bool | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    customer_contact: str | None = None


@router.put("/notify")
async def update_notify(body: NotifyUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    c = await _get_channel(db, "notify")
    data = body.model_dump(exclude_unset=True)
    settings_ = dict(c.settings)
    if "enabled" in data:
        c.enabled = data.pop("enabled")
    if "smtp_password" in data:
        pw = data.pop("smtp_password")
        if pw and pw != _MASK:
            settings_["smtp_password_enc"] = encrypt_secret(pw)
    settings_.update({k: v for k, v in data.items()})
    c.settings = settings_
    await write_audit(db, user, "channel.update", "channel", "notify", {})
    await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------- wechat
@router.get("/wechat")
async def get_wechat(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    c = await _get_channel(db, ChannelType.WECHAT_WORK)
    s = dict(c.settings)
    s["secret_enc"] = mask_secret(s.get("secret_enc"))
    s["encoding_aes_key"] = mask_secret(s.get("encoding_aes_key"))
    return {"id": str(c.id), "enabled": c.enabled, "settings": s}


class WeChatUpdate(BaseModel):
    enabled: bool | None = None
    corp_id: str | None = None
    agent_id: str | None = None
    token: str | None = None
    encoding_aes_key: str | None = None
    secret: str | None = None
    system_prompt_override: str | None = None


@router.put("/wechat")
async def update_wechat(body: WeChatUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    c = await _get_channel(db, ChannelType.WECHAT_WORK)
    data = body.model_dump(exclude_unset=True)
    settings_ = dict(c.settings)
    if "enabled" in data:
        c.enabled = data.pop("enabled")
    if "system_prompt_override" in data:
        c.system_prompt_override = data.pop("system_prompt_override")
    for secret_field in ("secret", "encoding_aes_key"):
        if secret_field in data:
            val = data.pop(secret_field)
            if val and val != _MASK:
                settings_[f"{secret_field}_enc" if secret_field == "secret" else secret_field] = (
                    encrypt_secret(val) if secret_field == "secret" else val
                )
    settings_.update({k: v for k, v in data.items()})
    c.settings = settings_
    await write_audit(db, user, "channel.update", "channel", "wechat_work", {})
    await db.commit()
    return {"ok": True}
