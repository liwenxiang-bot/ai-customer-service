"""Channel-config helpers: fetch the Web channel, enforce the embed domain whitelist."""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import ChannelConfig
from app.models.enums import ChannelType


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
        "enabled": channel.enabled,
    }
