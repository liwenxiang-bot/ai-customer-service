"""Idempotent first-boot seeding: default admin, AI config, web channel, notify config.

Run after migrations:  python -m scripts.bootstrap
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.session import session_scope
from app.models.admin import AdminUser
from app.models.config import ChannelConfig
from app.models.enums import AdminRole, ChannelType
from app.services.ai_config import get_active_ai_config
from app.services.channel import get_web_channel

log = get_logger("bootstrap")


async def _seed_admin(db) -> None:
    count = (await db.execute(select(func.count(AdminUser.id)))).scalar_one()
    if count:
        log.info("admin_exists_skip", count=count)
        return
    admin = AdminUser(
        email=settings.bootstrap_admin_email,
        name="超级管理员",
        password_hash=hash_password(settings.bootstrap_admin_password),
        role=AdminRole.ADMIN,
        is_active=True,
    )
    db.add(admin)
    log.info("admin_created", email=settings.bootstrap_admin_email)


async def _seed_notify(db) -> None:
    existing = (
        await db.execute(
            select(ChannelConfig).where(ChannelConfig.channel_type == "notify").limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return
    db.add(
        ChannelConfig(
            channel_type="notify",
            key="default",
            name="转人工通知",
            enabled=True,
            settings={
                "wechat_webhook_url": "",
                "email_to": "",
                "smtp_host": "",
                "smtp_port": 465,
                "smtp_ssl": True,
                "smtp_user": "",
                "smtp_password_enc": "",
                "smtp_from": "",
                "customer_contact": "如需紧急协助，可拨打客服热线 400-000-0000。",
            },
        )
    )
    log.info("notify_config_created")


async def _seed_wechat(db) -> None:
    existing = (
        await db.execute(
            select(ChannelConfig)
            .where(ChannelConfig.channel_type == ChannelType.WECHAT_WORK)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return
    db.add(
        ChannelConfig(
            channel_type=ChannelType.WECHAT_WORK,
            key="default",
            name="企业微信",
            enabled=False,
            settings={
                "corp_id": "",
                "agent_id": "",
                "token": "",
                "encoding_aes_key": "",
                "secret_enc": "",
            },
        )
    )
    log.info("wechat_config_created")


async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        await get_active_ai_config(db)   # creates the default AI config if missing
        await get_web_channel(db)        # creates the default web channel if missing
        await _seed_notify(db)
        await _seed_wechat(db)
        await _seed_admin(db)
    log.info("bootstrap_done")


if __name__ == "__main__":
    asyncio.run(main())
