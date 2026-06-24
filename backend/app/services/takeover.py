"""Live human takeover (坐席工作台 基础版).

An operator can take over a session: the AI pauses, the operator's replies are pushed
in real time to the customer's WebSocket via Redis pub/sub, and the customer's messages
are persisted for the operator to see. Releasing the takeover resumes the AI.

Cross-process delivery: the customer's WS may live in a different worker than the one
handling the operator's HTTP request, so messages are relayed over a Redis channel
(`chat:push:{session_id}`) that the WS endpoint subscribes to. A Redis flag
(`takeover:{session_id}`) is the fast per-message check on the chat hot path.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole, SessionStatus

log = get_logger("takeover")

_TTL = 60 * 60 * 24  # takeover flag auto-expires after 24h as a safety net


def push_channel(session_id: str) -> str:
    return f"chat:push:{session_id}"


def _flag_key(session_id: str) -> str:
    return f"takeover:{session_id}"


async def is_takeover(session_id: str) -> bool:
    if not session_id:
        return False
    try:
        return bool(await get_redis().exists(_flag_key(session_id)))
    except Exception:  # noqa: BLE001
        return False


async def publish(session_id: str, payload: dict) -> None:
    """Relay an event to the customer's live WebSocket (if connected)."""
    try:
        await get_redis().publish(push_channel(session_id), json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        log.warning("takeover_publish_failed", error=str(exc))


async def start_takeover(db: AsyncSession, session: Session) -> None:
    session.status = SessionStatus.HUMAN_TAKEOVER
    await db.flush()
    await get_redis().set(_flag_key(str(session.id)), "1", ex=_TTL)
    await publish(str(session.id), {"type": "human_takeover", "message": "人工客服已接入，正在为你服务~"})
    log.info("takeover_started", session_id=str(session.id))


async def end_takeover(db: AsyncSession, session: Session, resume_ai: bool = True) -> None:
    session.status = SessionStatus.ACTIVE if resume_ai else SessionStatus.HUMAN_HANDLED
    await db.flush()
    await get_redis().delete(_flag_key(str(session.id)))
    await publish(
        str(session.id),
        {
            "type": "ai_resumed" if resume_ai else "human_ended",
            "message": "AI 助手已恢复，继续为你服务。" if resume_ai else "本次人工服务已结束，感谢咨询。",
        },
    )
    log.info("takeover_ended", session_id=str(session.id), resume_ai=resume_ai)


async def operator_reply(db: AsyncSession, session: Session, content: str) -> DBMessage:
    """Persist an operator message and push it live to the customer."""
    session.message_count += 1
    session.last_activity_at = datetime.now(UTC)
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=session.message_count,
        role=MessageRole.ASSISTANT,
        content=content,
        model="human",  # marks this bubble as a human agent (not the AI)
    )
    db.add(msg)
    await db.flush()
    await publish(
        str(session.id),
        {"type": "human_message", "content": content, "message_id": str(msg.id)},
    )
    return msg


async def persist_customer_message(
    db: AsyncSession, session_id: str, text: str, attachments: list | None = None
) -> DBMessage | None:
    """Persist a customer message during takeover (AI is paused, so no agent turn)."""
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        return None
    session = await db.get(Session, sid)
    if not session:
        return None
    session.message_count += 1
    session.last_activity_at = datetime.now(UTC)
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=session.message_count,
        role=MessageRole.USER,
        content=text,
        attachments=attachments or [],
    )
    db.add(msg)
    await db.flush()
    return msg
