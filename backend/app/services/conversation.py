"""ConversationService — the channel-agnostic turn handler.

Given a normalized InboundMessage it: resolves config, enforces the cost circuit
breaker, gets/creates the session, persists the user message, runs the AgentRunner,
and yields AgentEvents. Channels render those events; they never touch the agent core.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.events import AgentEvent
from app.agent.runner import AgentRunner
from app.agent.tools import default_registry
from app.channels.base import InboundMessage
from app.core.logging import get_logger, set_trace_id
from app.llm.factory import get_provider
from app.models.config import ChannelConfig
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole, SessionStatus
from app.services import semantic_cache
from app.services.ai_config import get_active_ai_config, to_llm_settings
from app.services.content_safety import check_input
from app.services.usage import is_cost_capped, mark_escalation, mark_new_conversation

log = get_logger("conversation")


async def get_or_create_session(db: AsyncSession, inbound: InboundMessage) -> tuple[Session, bool]:
    if inbound.session_id:
        existing = await db.get(Session, _as_uuid(inbound.session_id))
        if existing and existing.status != SessionStatus.CLOSED:
            return existing, False

    session = Session(
        channel_type=inbound.channel_type,
        channel_key=inbound.channel_key,
        end_user_id=inbound.end_user_id,
        end_user_display=inbound.end_user_display,
        status=SessionStatus.ACTIVE,
        last_activity_at=datetime.now(UTC),
        meta=inbound.meta,
    )
    db.add(session)
    await db.flush()
    await mark_new_conversation(db, session)
    return session, True


async def _channel_runtime(db: AsyncSession, inbound: InboundMessage) -> tuple[str | None, bool]:
    """Return (system_prompt_override, image_understanding_enabled) for this channel."""
    row = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.channel_type == inbound.channel_type,
                ChannelConfig.key == inbound.channel_key,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return None, False
    return row.system_prompt_override, bool(row.settings.get("image_understanding_enabled", False))


async def _persist_user_message(
    db: AsyncSession, session: Session, text: str, attachments: list | None = None
) -> DBMessage:
    session.message_count += 1
    session.last_activity_at = datetime.now(UTC)
    if not session.title:
        session.title = (text or "[附件]")[:80]
    if session.status == SessionStatus.IDLE:
        session.status = SessionStatus.ACTIVE
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


async def _persist_assistant_text(
    db: AsyncSession, session: Session, content: str, citations: list, cache_hit: bool = False
) -> DBMessage:
    session.message_count += 1
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=session.message_count,
        role=MessageRole.ASSISTANT,
        content=content,
        citations=citations or [],
        tool_calls=[{"name": "semantic_cache", "status": "ok"}] if cache_hit else [],
    )
    db.add(msg)
    await db.flush()
    return msg


async def handle_turn(
    db: AsyncSession, inbound: InboundMessage
) -> AsyncGenerator[AgentEvent, None]:
    trace_id = uuid.uuid4().hex
    set_trace_id(trace_id)

    ai_config = await get_active_ai_config(db)
    session, _ = await get_or_create_session(db, inbound)

    await _persist_user_message(db, session, inbound.text, inbound.attachments)

    # ---- Input content safety (optional) ----
    safe, notice = await check_input(ai_config, inbound.text)
    if not safe:
        yield AgentEvent(kind="text", text=notice)
        yield AgentEvent(kind="done", text=notice, data={"message_id": None, "blocked": True})
        await db.commit()
        return

    # ---- Cost circuit breaker ----
    if await is_cost_capped():
        notice = "当前服务咨询量较大，智能客服暂时繁忙，请稍后再试，或回复「人工」由同事跟进。"
        yield AgentEvent(kind="text", text=notice)
        yield AgentEvent(kind="done", text=notice, data={"message_id": None, "capped": True})
        await db.commit()
        log.warning("turn_rejected_cost_capped", session_id=str(session.id))
        return

    # ---- Semantic cache: short-circuit repeated FAQs (optional) ----
    cached = await semantic_cache.lookup(db, ai_config, inbound.text)
    if cached:
        await _persist_assistant_text(db, session, cached["answer"], cached["citations"], cache_hit=True)
        yield AgentEvent(kind="text", text=cached["answer"])
        if cached["citations"]:
            yield AgentEvent(kind="citations", citations=cached["citations"])
        yield AgentEvent(
            kind="done", text=cached["answer"],
            data={"message_id": None, "session_id": str(session.id),
                  "citations": cached["citations"], "cache_hit": True},
        )
        await db.commit()
        return

    provider = get_provider(to_llm_settings(ai_config))
    channel_prompt, image_understanding = await _channel_runtime(db, inbound)
    runner = AgentRunner(provider, default_registry, ai_config, channel_prompt, image_understanding)

    escalated = False
    final_text = ""
    final_citations: list = []
    degraded = False
    async for ev in runner.run_turn(db, session, trace_id):
        if ev.kind == "escalation":
            escalated = True
        elif ev.kind == "done":
            final_text = ev.text
            final_citations = ev.data.get("citations", [])
            degraded = ev.data.get("degraded", False)
            _maybe_trace(trace_id, session, inbound, ev, ai_config)
        yield ev

    if escalated:
        await mark_escalation(db, session)

    # Cache successful, grounded, non-escalated answers for future hits.
    if not escalated and not degraded and final_citations and final_text:
        await semantic_cache.store(db, ai_config, inbound.text, final_text, final_citations)

    await db.commit()


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _maybe_trace(trace_id, session, inbound, ev, ai_config) -> None:
    """Send an optional Langfuse trace (no-op unless enabled)."""
    from app.core.tracing import trace_turn

    usage = ev.data.get("usage") or {}
    trace_turn(
        trace_id,
        session_id=str(session.id),
        model=ai_config.llm_model,
        input_text=inbound.text,
        output_text=ev.text,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cost_usd=ev.data.get("cost_usd", 0.0),
        latency_ms=ev.data.get("latency_ms", 0),
        tool_calls=[],
        citations=ev.data.get("citations", []),
    )
