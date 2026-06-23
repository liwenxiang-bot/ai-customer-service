"""Outbound operator notifications for handoff (WeChat Work group bot / email).

Reads notification settings from a ChannelConfig row (channel_type='notify'). Every
send is best-effort and returns (ok, error) so the caller records the outcome on the
ticket rather than failing the conversation.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.mime.text import MIMEText

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_secret
from app.core.logging import get_logger
from app.models.config import ChannelConfig

log = get_logger("notify")


async def get_notify_config(db: AsyncSession) -> dict:
    row = (
        await db.execute(
            select(ChannelConfig).where(ChannelConfig.channel_type == "notify").limit(1)
        )
    ).scalar_one_or_none()
    return dict(row.settings) if row and row.settings else {}


async def _send_wechat_webhook(url: str, title: str, body_md: str) -> tuple[bool, str]:
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"### {title}\n{body_md}"},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errcode", 0) != 0:
                return False, f"wechat errcode={data.get('errcode')}: {data.get('errmsg')}"
            return True, ""
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"wechat webhook failed: {exc}"


def _send_email_sync(cfg: dict, subject: str, body: str) -> tuple[bool, str]:
    host = cfg.get("smtp_host")
    if not host:
        return False, "smtp not configured"
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = cfg.get("smtp_from", cfg.get("smtp_user", ""))
        msg["To"] = cfg.get("email_to", "")
        port = int(cfg.get("smtp_port", 465))
        if cfg.get("smtp_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        if cfg.get("smtp_user"):
            server.login(cfg["smtp_user"], decrypt_secret(cfg.get("smtp_password_enc")) or "")
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())
        server.quit()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"email failed: {exc}"


async def notify_operator(db: AsyncSession, title: str, body_md: str) -> tuple[bool, str]:
    """Try every configured channel; succeed if at least one delivers."""
    cfg = await get_notify_config(db)
    delivered = False
    errors: list[str] = []

    webhook = cfg.get("wechat_webhook_url")
    if webhook:
        ok, err = await _send_wechat_webhook(webhook, title, body_md)
        delivered = delivered or ok
        if err:
            errors.append(err)

    if cfg.get("email_to") and cfg.get("smtp_host"):
        ok, err = await asyncio.to_thread(_send_email_sync, cfg, title, body_md)
        delivered = delivered or ok
        if err:
            errors.append(err)

    if not webhook and not cfg.get("email_to"):
        errors.append("no notification channel configured")

    if not delivered:
        log.warning("operator_notify_failed", errors=errors)
    return delivered, "; ".join(errors)
