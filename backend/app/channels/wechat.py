"""WeChat Work channel adapter: load config, build the crypto, normalize inbound."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundMessage
from app.channels.wechat_crypto import WXBizMsgCrypt, parse_message_xml
from app.core.encryption import decrypt_secret
from app.models.config import ChannelConfig
from app.models.enums import ChannelType


@dataclass
class WeChatConfig:
    enabled: bool
    corp_id: str
    agent_id: str
    token: str
    encoding_aes_key: str
    secret: str
    system_prompt_override: str | None


async def load_wechat_config(db: AsyncSession, key: str = "default") -> WeChatConfig | None:
    row = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == ChannelType.WECHAT_WORK, ChannelConfig.key == key
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None
    s = row.settings or {}
    return WeChatConfig(
        enabled=row.enabled,
        corp_id=s.get("corp_id", ""),
        agent_id=s.get("agent_id", ""),
        token=s.get("token", ""),
        encoding_aes_key=s.get("encoding_aes_key", ""),
        secret=decrypt_secret(s.get("secret_enc")) or "",
        system_prompt_override=row.system_prompt_override,
    )


def build_crypto(cfg: WeChatConfig) -> WXBizMsgCrypt:
    return WXBizMsgCrypt(cfg.token, cfg.encoding_aes_key, cfg.corp_id)


def inbound_from_wechat(msg: dict) -> InboundMessage | None:
    """Map a decrypted WeChat text message to a normalized InboundMessage.

    Group messages only trigger when the bot is @-mentioned (the platform strips the
    mention and sets the relevant fields); plain single-chat text always triggers.
    """
    if msg.get("MsgType") != "text":
        return None
    content = (msg.get("Content") or "").strip()
    if not content:
        return None
    from_user = msg.get("FromUserName", "")
    return InboundMessage(
        channel_type=ChannelType.WECHAT_WORK,
        channel_key="default",
        end_user_id=from_user,
        end_user_display=from_user,
        text=content,
        meta={"msg_id": msg.get("MsgId", ""), "agent_id": msg.get("AgentID", "")},
    )


def parse_inbound(xml_text: str) -> dict:
    return parse_message_xml(xml_text)
