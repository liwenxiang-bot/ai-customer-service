"""Channel-config helpers: fetch the Web channel, enforce the embed domain whitelist."""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tenant_context import get_current_tenant
from app.models.config import ChannelConfig
from app.models.enums import ChannelType


async def get_tenant_web_channel(db: AsyncSession) -> ChannelConfig:
    """The CURRENT tenant's web channel, found by type rather than key.

    A tenant's web channel is keyed by its slug (so the public widget can resolve the tenant
    from channel_key); the default tenant's key is 'default'. The admin UI must read/write THIS
    row — not a hard-coded key='default' — otherwise (for any non-default tenant) its branding
    is written to a row the widget never reads. RLS already scopes the query to this tenant."""
    row = (
        await db.execute(
            select(ChannelConfig)
            .where(ChannelConfig.channel_type == ChannelType.WEB)
            .order_by(ChannelConfig.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    # No web channel yet (pre-provisioning / fresh default) — key it by the tenant slug.
    tid = get_current_tenant()
    slug = "default"
    if tid:
        slug = (
            await db.execute(text("SELECT slug FROM tenants WHERE id = :i"), {"i": str(tid)})
        ).scalar() or "default"
    row = ChannelConfig(
        channel_type=ChannelType.WEB, key=slug, name="Web 对话窗口",
        enabled=True, settings=ChannelConfig.default_web_settings(), allowed_domains=[],
    )
    db.add(row)
    await db.flush()
    return row


async def get_web_channel(db: AsyncSession, key: str = "default") -> ChannelConfig:
    row = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == ChannelType.WEB,
                ChannelConfig.key == key,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        row = ChannelConfig(
            channel_type=ChannelType.WEB,
            key=key,
            name="Web 对话窗口",
            enabled=True,
            settings=ChannelConfig.default_web_settings(),
            allowed_domains=[],
        )
        db.add(row)
        await db.flush()
    return row


async def resolve_active_web_channel(db: AsyncSession, channel_key: str) -> ChannelConfig | None:
    """Public widget/chat path: return the web channel ONLY if it maps to an ENABLED channel of
    an ACTIVE tenant; otherwise None (caller rejects). Unlike get_web_channel this never
    auto-creates, so an unknown / disabled / suspended channel_key can't silently fall back to —
    or pollute — the default tenant."""
    from app.services.tenant import tenant_for_channel

    tid = await tenant_for_channel(db, "web", channel_key)
    if tid is None:
        return None
    return await get_web_channel(db, channel_key)


def _host_of(origin: str) -> str:
    if not origin:
        return ""
    parsed = urlparse(origin if "://" in origin else f"//{origin}")
    return (parsed.hostname or "").lower()


def is_origin_allowed(channel: ChannelConfig, origin: str) -> bool:
    """Whitelist check. Empty list = allow all (dev convenience). Supports exact host
    and wildcard subdomains ('*.example.com'). localhost is always allowed."""
    domains = channel.allowed_domains or []
    if not domains:
        return True
    host = _host_of(origin)
    if not host:
        return False
    if host in ("localhost", "127.0.0.1"):
        return True
    for d in domains:
        d = d.strip().lower()
        if not d:
            continue
        if d.startswith("*."):
            if host == d[2:] or host.endswith(d[1:]):
                return True
        elif host == d:
            return True
    return False


def public_branding(channel: ChannelConfig) -> dict:
    """Safe subset of channel settings exposed to the public widget."""
    s = channel.settings or {}
    return {
        "welcome_message": s.get("welcome_message", ""),
        "theme_color": s.get("theme_color", "#4f46e5"),
        "logo_url": s.get("logo_url", ""),
        "brand_name": s.get("brand_name", "智能客服"),
        "placeholder": s.get("placeholder", "输入你的问题…"),
        "default_theme": s.get("default_theme", "light"),
        "show_powered_by": s.get("show_powered_by", True),
        "image_understanding_enabled": s.get("image_understanding_enabled", False),
        "file_upload_enabled": s.get("file_upload_enabled", True),
        "suggested_questions": s.get("suggested_questions", []),
        "enabled": channel.enabled,
    }
