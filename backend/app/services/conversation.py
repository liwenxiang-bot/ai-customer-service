"""ConversationService — the channel-agnostic turn handler.

Given a normalized InboundMessage it: resolves config, enforces the cost circuit
breaker, gets/creates the session, persists the user message, runs the AgentRunner,
and yields AgentEvents. Channels render those events; they never touch the agent core.

Transactions: one turn spans TWO transactions on the caller's db session. #1 commits
the user message (with session creation, seq and client_msg_id bookkeeping) before the
AI half starts; #2 covers everything the AI half writes (assistant message, usage,
escalation, semantic cache). A disconnect / user stop that cancels the turn therefore
only rolls back #2 — the customer's message is never lost with it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.events import AgentEvent
from app.agent.runner import AgentRunner
from app.agent.tools import default_registry
from app.channels.base import InboundMessage
from app.config import settings
from app.core.logging import get_logger, set_trace_id
from app.core.metrics import guard_blocks
from app.llm.factory import get_provider
from app.models.config import ChannelConfig
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import ChannelType, MessageRole, SessionStatus
from app.services import abuse_guard, semantic_cache
from app.services.ai_config import get_active_ai_config, to_llm_settings
from app.services.content_safety import check_input
from app.services.takeover import is_takeover, publish
from app.services.usage import is_cost_capped, mark_escalation, mark_new_conversation

log = get_logger("conversation")


async def owned_session(
    db: AsyncSession, session_id: str | None, end_user_id: str, channel_type: str | None = None
) -> Session | None:
    """The session, only if it belongs to this end user (and channel, when given).

    A session id alone is not a credential: it shows up in operator deep links, admin URLs
    and logs. The end user's id (a random per-browser uid for the widget, the openid for
    WeChat) must match too, otherwise the caller is treated as having no session.
    """
    sid = _as_uuid(session_id or "")
    if sid is None or not end_user_id:
        return None
    s = await db.get(Session, sid)
    if s is None or s.end_user_id != end_user_id:
        return None
    if channel_type and s.channel_type != channel_type:
        return None
    return s


async def get_or_create_session(db: AsyncSession, inbound: InboundMessage) -> tuple[Session, bool]:
    if inbound.session_id:
        existing = await owned_session(
            db, inbound.session_id, inbound.end_user_id, inbound.channel_type
        )
        if existing and existing.status != SessionStatus.CLOSED:
            return existing, False
        if existing is None:
            log.warning("session_owner_mismatch", session_id=inbound.session_id,
                        channel=inbound.channel_type)
    elif inbound.end_user_id and inbound.channel_type != ChannelType.WEB:
        # Channels without a client-held session id (WeChat): continue the user's latest open
        # conversation within the idle window, instead of starting a new session — and losing
        # all context — on every single message.
        cutoff = datetime.now(UTC) - timedelta(minutes=settings.session_idle_timeout_minutes)
        recent = (
            await db.execute(
                select(Session)
                .where(
                    Session.channel_type == inbound.channel_type,
                    Session.channel_key == inbound.channel_key,
                    Session.end_user_id == inbound.end_user_id,
                    Session.status != SessionStatus.CLOSED,
                    Session.last_activity_at >= cutoff,
                )
                .order_by(Session.last_activity_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if recent is not None:
            return recent, False

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


def _record_client_msg_id(session: Session, client_msg_id: str, message_id: str) -> None:
    """Remember client_msg_id → message id on the session (bounded, newest last).

    The widget sends a client-generated id with each user message so it can match its
    optimistic bubble to the persisted row (ACK, history merge) and so retries can be
    deduplicated server-side. Message rows have no free-form JSON column to spare, so
    the mapping lives in `session.meta` — an ordered list of [client_msg_id, message_id]
    pairs (JSONB objects don't preserve key order); reassigned so SQLAlchemy sees the change.
    """
    pairs = [
        p for p in (session.meta or {}).get("client_msg_ids", []) if p and p[0] != client_msg_id
    ]
    pairs.append([client_msg_id, message_id])
    session.meta = {**(session.meta or {}), "client_msg_ids": pairs[-200:]}


async def next_seq(db: AsyncSession, session: Session) -> int:
    """Allocate the next per-session message seq atomically.

    seq must be monotonic and unique within a session, but concurrent writers exist:
    an operator reply can land while an AI turn (started before takeover) is still
    streaming, and duplicate widget tabs share one session. A Python-level
    `message_count += 1` flushes `SET message_count = <literal>`, so two writers both
    read N and both write N+1 → duplicate seq, count under-counted. Push the increment
    into a single atomic UPDATE ... RETURNING so Postgres serializes it; the ORM object
    is synced to the returned value for callers that read session.message_count.
    """
    result = await db.execute(
        update(Session)
        .where(Session.id == session.id)
        .values(message_count=Session.message_count + 1)
        .returning(Session.message_count)
    )
    seq = result.scalar_one()
    session.message_count = seq
    return seq


async def _persist_user_message(
    db: AsyncSession,
    session: Session,
    text: str,
    attachments: list | None = None,
    client_msg_id: str = "",
) -> DBMessage:
    seq = await next_seq(db, session)
    session.last_activity_at = datetime.now(UTC)
    if not session.title:
        session.title = (text or "[附件]")[:80]
    if session.status == SessionStatus.IDLE:
        session.status = SessionStatus.ACTIVE
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
    if client_msg_id:
        _record_client_msg_id(session, client_msg_id, str(msg.id))
    return msg


async def _persist_assistant_text(
    db: AsyncSession, session: Session, content: str, citations: list, cache_hit: bool = False
) -> DBMessage:
    seq = await next_seq(db, session)
    msg = DBMessage(
        tenant_id=session.tenant_id,
        session_id=session.id,
        seq=seq,
        role=MessageRole.ASSISTANT,
        content=content,
        citations=citations or [],
        tool_calls=[{"name": "semantic_cache", "status": "ok"}] if cache_hit else [],
    )
    db.add(msg)
    await db.flush()
    return msg


async def handle_turn(
    db: AsyncSession, inbound: InboundMessage, client_msg_id: str = ""
) -> AsyncGenerator[AgentEvent, None]:
    trace_id = uuid.uuid4().hex
    set_trace_id(trace_id)

    ai_config = await get_active_ai_config(db)

    # ---- Guard layer 1: length cap (before anything is stored or sent to the LLM) ----
    cap = abuse_guard.too_long(ai_config, inbound.text)
    if cap:
        guard_blocks.labels("too_long").inc()
        notice = f"消息有点长啦，请精简到 {cap} 字以内再发给我～"
        yield AgentEvent(kind="text", text=notice)
        yield AgentEvent(
            kind="done", text=notice,
            data={"message_id": None, "session_id": inbound.session_id, "blocked": True},
        )
        return

    session, _ = await get_or_create_session(db, inbound)

    user_msg = await _persist_user_message(
        db, session, inbound.text, inbound.attachments, client_msg_id
    )
    # ---- Transaction #1: make the user message durable BEFORE the AI half runs ----
    # Everything above (session creation, seq, client_msg_id bookkeeping, usage "new
    # conversation" tick) commits here so that a mid-turn cancel/disconnect only rolls
    # back the AI transaction below — the customer's message survives (all channels:
    # WS, REST and WeChat share this generator, so the behavior is identical).
    # The resolved session id is written back onto `inbound` first: cancellation
    # handlers (ws._persist_stopped_turn) must resume THIS session instead of creating
    # a fresh one, and no event has been yielded yet at this point. The ORM objects
    # stay usable after commit (expire_on_commit=False) and the tenant GUC is re-pinned
    # on the next transaction's BEGIN (db.session._pin_tenant_on_begin).
    inbound.session_id = str(session.id)
    await db.commit()

    # ---- Human takeover: the AI stays silent on EVERY channel ----
    # (The WebSocket path checks this before calling us; REST and WeChat rely on this gate.)
    # The message is already persisted; surface it live to the operator and stop here.
    if await is_takeover(str(session.id)):
        await publish(
            str(session.id),
            {
                "type": "customer_message",
                "session_id": str(session.id),
                "content": inbound.text,
                "message_id": str(user_msg.id),
                "attachments": inbound.attachments or [],
            },
        )
        yield AgentEvent(
            kind="done", text="",
            data={"message_id": None, "session_id": str(session.id), "takeover": True},
        )
        return

    # ---- Guard layer 2: obvious jailbreak / prompt-injection phrasing → canned refusal ----
    if abuse_guard.is_jailbreak(ai_config, inbound.text):
        guard_blocks.labels("jailbreak").inc()
        log.warning("guard_jailbreak_blocked", session_id=str(session.id))
        reply = abuse_guard.off_topic_reply(ai_config)
        msg = await _persist_assistant_text(db, session, reply, [])
        yield AgentEvent(kind="text", text=reply)
        yield AgentEvent(
            kind="done", text=reply,
            data={"message_id": str(msg.id), "session_id": str(session.id), "blocked": True},
        )
        await db.commit()
        return

    # ---- Input content safety (optional) ----
    safe, notice = await check_input(ai_config, inbound.text)
    if not safe:
        yield AgentEvent(kind="text", text=notice)
        yield AgentEvent(kind="done", text=notice, data={"message_id": None, "blocked": True})
        await db.commit()  # nothing written since #1; kept so future writes here commit
        return

    # ---- Cost circuit breaker ----
    if await is_cost_capped():
        notice = "当前服务咨询量较大，智能客服暂时繁忙，请稍后再试，或回复「人工」由同事跟进。"
        yield AgentEvent(kind="text", text=notice)
        yield AgentEvent(kind="done", text=notice, data={"message_id": None, "capped": True})
        await db.commit()  # nothing written since #1; kept so future writes here commit
        log.warning("turn_rejected_cost_capped", session_id=str(session.id))
        return

    # ---- Semantic cache: short-circuit repeated FAQs (optional) ----
    # Attachment turns depend on uploaded content, so a text-only semantic-cache hit would
    # be stale or outright wrong for prompts like "这个是啥". Context-dependent text
    # ("它多少钱") is likewise skipped — on both read and write — inside semantic_cache,
    # since the referent differs per conversation.
    # Cache reads/writes belong to transaction #2: the hit_count bump and the cached
    # assistant message roll back together if this half of the turn is cancelled.
    cached = None
    if inbound.text and not inbound.attachments:
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
        await db.commit()  # transaction #2 (cache-hit turn: assistant message + hit_count)
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

    # Cache successful, grounded, non-escalated answers for future hits. Deliberately
    # still inside transaction #2: a cancelled turn must not cache an answer whose
    # assistant message was rolled back. (The summarize task enqueued by the runner is
    # queue-based and reads committed data in its own session — no tx coupling needed.)
    if not escalated and not degraded and final_citations and final_text:
        await semantic_cache.store(db, ai_config, inbound.text, final_text, final_citations)

    await db.commit()  # transaction #2: assistant message, usage/cost, escalation, cache


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
