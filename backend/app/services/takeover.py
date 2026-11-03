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


def admin_channel(tenant_id) -> str:
    """Per-tenant admin event bus — operators only see their own tenant's queue events."""
    return f"admin:events:{tenant_id}"


async def publish_admin_event(payload: dict) -> None:
    """Broadcast a backend event to the current tenant's admin/operator workbench clients."""
    from app.db.tenant_context import DEFAULT_TENANT_ID, get_current_tenant

    tid = get_current_tenant() or DEFAULT_TENANT_ID
    try:
        await get_redis().publish(admin_channel(tid), json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        log.warning("admin_event_publish_failed", error=str(exc))


def _flag_key(session_id: str) -> str:
    return f"takeover:{session_id}"


async def clear_takeover_flag(session_id: str) -> None:
    """Best-effort 删除 Redis 接管标志。所有让会话离队的路径都必须清它：
    is_takeover 只看这个标志（不查 DB），残留会让 AI 对该会话哑火最长 24h（TTL）。
    Redis 抖动不阻断主流程——标志最终会因 TTL 过期自愈。"""
    try:
        await get_redis().delete(_flag_key(session_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("takeover_flag_cleanup_failed", session_id=session_id, error=str(exc))


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


async def start_takeover(db: AsyncSession, session: Session, operator=None) -> None:
    session.status = SessionStatus.HUMAN_TAKEOVER
    if operator is not None:
        # Attribute the takeover so queue-counts can answer "接待中（我的）". Written to
        # dedicated columns (not session.meta) so this doesn't read-modify-write the whole
        # JSONB blob and lose the concurrent client_msg_id bookkeeping.
        session.takeover_by_id = operator.id
        session.takeover_by_name = operator.name or operator.email
    await db.flush()
    await get_redis().set(_flag_key(str(session.id)), "1", ex=_TTL)
    await publish(str(session.id), {"type": "human_takeover", "message": "人工客服已接入，正在为你服务~"})
    await publish_admin_event({"type": "queue", "event": "takeover", "session_id": str(session.id)})
    log.info("takeover_started", session_id=str(session.id))


async def end_takeover(
    db: AsyncSession, session: Session, resume_ai: bool = True, operator=None
) -> list[str]:
    """End the takeover and auto-resolve the session's open/in_progress handoff
    tickets — in BOTH branches. resume_ai=True 时人工介入同样已经结束，工单若继续
    挂在坐席名下，会话（已回 active）却不在任何队列列表里，只会把 queue-counts
    的 mine 角标撑成幽灵计数。客户后续再求助会重新建单。Returns resolved ids."""
    session.status = SessionStatus.ACTIVE if resume_ai else SessionStatus.HUMAN_HANDLED
    # Clear the takeover attribution so the session drops out of "接待中（我的）".
    session.takeover_by_id = None
    session.takeover_by_name = ""
    # Lazy import: handoff 模块级 import 了本模块（publish_admin_event）。
    from app.services.handoff import resolve_session_tickets

    resolved_ids = await resolve_session_tickets(
        db,
        session,
        "坐席结束接管（AI 接续）" if resume_ai else "坐席结束会话",
        assignee_id=operator.id if operator is not None else None,
    )
    await db.flush()
    await get_redis().delete(_flag_key(str(session.id)))
    await publish(
        str(session.id),
        {
            "type": "ai_resumed" if resume_ai else "human_ended",
            "message": "AI 助手已恢复，继续为你服务。" if resume_ai else "本次人工服务已结束，感谢咨询。",
        },
    )
    await publish_admin_event({"type": "queue", "event": "released", "session_id": str(session.id)})
    log.info(
        "takeover_ended",
        session_id=str(session.id),
        resume_ai=resume_ai,
        resolved_tickets=len(resolved_ids),
    )
    return resolved_ids


async def close_session(
    db: AsyncSession, session: Session, rating: int = 0, note: str = ""
) -> list[str]:
    """客户端结束会话：置 CLOSED、落满意度，并收尾人工侧状态——解决存活工单、
    清接管标志与归属，让工作台队列与角标即时回落。此前这里只改会话状态，
    坐席已认领的工单会永远滞留 in_progress（幽灵计数）。Returns resolved ids."""
    was_takeover = session.status == SessionStatus.HUMAN_TAKEOVER
    session.status = SessionStatus.CLOSED
    if 1 <= rating <= 5:
        session.satisfaction_rating = rating
    if note:
        session.satisfaction_note = note
    session.takeover_by_id = None
    session.takeover_by_name = ""
    from app.services.handoff import resolve_session_tickets  # lazy: 避免循环 import

    resolved_ids = await resolve_session_tickets(db, session, "客户结束会话")
    await db.flush()
    if was_takeover:
        await clear_takeover_flag(str(session.id))
    if resolved_ids or was_takeover:
        await publish_admin_event(
            {"type": "queue", "event": "closed", "session_id": str(session.id)}
        )
    log.info(
        "session_closed_by_customer",
        session_id=str(session.id),
        resolved_tickets=len(resolved_ids),
        was_takeover=was_takeover,
    )
    return resolved_ids


async def operator_reply(
    db: AsyncSession, session: Session, content: str, attachments: list[dict] | None = None
) -> DBMessage:
    """Persist an operator message (text and/or attachments) and push it live to the customer."""
    # Atomic per-session seq: an operator reply can race an AI turn that started before
    # takeover but is still streaming, so a Python-level `message_count += 1` would
    # collide (duplicate seq / undercount). next_seq pushes the increment into Postgres.
    # Imported lazily to avoid a circular import (conversation → agent → handoff → takeover).
    from app.services.conversation import next_seq

    seq = await next_seq(db, session)
    session.last_activity_at = datetime.now(UTC)
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=seq,
        role=MessageRole.ASSISTANT,
        content=content,
        attachments=attachments or [],
        model="human",  # marks this bubble as a human agent (not the AI)
    )
    db.add(msg)
    await db.flush()
    await publish(
        str(session.id),
        {
            "type": "human_message",
            # session_id lets subscribers (e.g. the workbench watching several sessions)
            # drop pushes that don't belong to the conversation on screen.
            "session_id": str(session.id),
            "content": content,
            "attachments": attachments or [],
            "message_id": str(msg.id),
        },
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
    from app.services.conversation import next_seq  # lazy: avoid circular import

    seq = await next_seq(db, session)
    session.last_activity_at = datetime.now(UTC)
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=seq,
        role=MessageRole.USER,
        content=text,
        attachments=attachments or [],
    )
    db.add(msg)
    await db.flush()
    await publish(
        session_id,
        {
            "type": "customer_message",
            "session_id": str(session.id),
            "content": text,
            "message_id": str(msg.id),
            "attachments": attachments or [],
        },
    )
    return msg
