"""Human handoff: create a ticket, notify the operator, mark the session.

Lightweight by design (requirements §8): notify + record + tell the customer. The
ticket schema is rich enough to grow into a full agent workbench later without a
rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.metrics import escalations as escalations_metric
from app.models.conversation import HandoffTicket, Session
from app.models.enums import HandoffStatus, SessionStatus
from app.services.notify import notify_operator
from app.services.takeover import publish, publish_admin_event

log = get_logger("handoff")

#: 会话仍在人工队列中的两个状态（待人工 / 接管中）。角标计数、列表过滤与
#: 工单收尾判断共用这一口径，避免各处 in_() 字面量再度漂移。
QUEUE_STATUSES = (SessionStatus.ESCALATED, SessionStatus.HUMAN_TAKEOVER)


async def live_tickets(db: AsyncSession, session_id) -> list[HandoffTicket]:
    """该会话所有存活（open/in_progress）工单，按创建时间倒序。"""
    return list(
        (
            await db.execute(
                select(HandoffTicket)
                .where(
                    HandoffTicket.session_id == session_id,
                    HandoffTicket.status.in_([HandoffStatus.OPEN, HandoffStatus.IN_PROGRESS]),
                )
                .order_by(HandoffTicket.created_at.desc())
            )
        ).scalars().all()
    )


async def resolve_session_tickets(
    db: AsyncSession, session: Session, note: str, assignee_id=None
) -> list[str]:
    """会话离开人工队列时的统一工单收尾：把全部存活工单置为已解决。

    历史上「客户结束会话 / 坐席结束接管(AI 恢复) / 标记完成」都不收尾工单，
    留下的 in_progress 孤儿只进 queue-counts 的 mine 角标、不进任何列表，
    造成「角标虚高、列表为空」。所有让会话离队的终结路径都必须走这里。
    assignee_id 把无人认领的工单归到操作者名下，便于事后追溯。
    """
    tickets = (
        await db.execute(
            select(HandoffTicket)
            .where(
                HandoffTicket.session_id == session.id,
                HandoffTicket.status.in_([HandoffStatus.OPEN, HandoffStatus.IN_PROGRESS]),
            )
            .with_for_update()
        )
    ).scalars().all()
    now = datetime.now(UTC)
    resolved_ids: list[str] = []
    for t in tickets:
        t.status = HandoffStatus.RESOLVED
        t.resolution_note = note
        t.resolved_at = now
        if assignee_id is not None and t.assignee_id is None:
            t.assignee_id = assignee_id
        resolved_ids.append(str(t.id))
    if resolved_ids:
        await db.flush()
    return resolved_ids


def agent_console_base() -> str:
    """Resolved base URL of the agent console (坐席工作台).

    Prefers the dedicated AGENT_CONSOLE_URL override (独立子域名部署); otherwise the
    backend serves the console under {app_base_url}/agent. Shared by deep links and by
    the admin /me response so the admin's console entry always points to the right place.
    """
    base = (settings.agent_console_url or "").rstrip("/")
    return base or f"{settings.app_base_url.rstrip('/')}/agent"


def agent_console_link(session_id) -> str:
    """Deep link into the agent console for a session (契约 C: …/#/s/{session_id}).
    HashRouter deep link — no backend fallback route needed."""
    return f"{agent_console_base()}/#/s/{session_id}"


def _admin_console_link(session_id) -> str:
    base = (settings.admin_base_url or settings.app_base_url).rstrip("/")
    return f"{base}/conversations?session={session_id}"


async def create_handoff(
    db: AsyncSession,
    session: Session,
    reason: str,
    reason_detail: str,
    summary: str,
) -> HandoffTicket:
    # 幂等：会话已有存活工单时直接复用——用户反复点「转人工」、模型在已升级会话上
    # 再调 escalate 工具都会走到这里。不查重会积累多张工单：重复通知轰炸运营，
    # 两个坐席还可能各认领一张、同时跟进同一个客户。
    existing = await live_tickets(db, session.id)
    if existing:
        ticket = existing[0]
        session.escalated = True
        requeued = session.status not in QUEUE_STATUSES
        if requeued:
            session.status = SessionStatus.ESCALATED
        await db.flush()
        # 复用分支是所有重复升级的汇聚点：原通知失败的在这里补发一次，否则企微
        # 通道对该会话永久静默（唯一恢复手段是后台人工点重发），客户却反复收到
        # 「已为你转接」的回执。
        if not ticket.notified:
            _notify_after_commit(db, ticket)
        if requeued:
            # 防御路径（如工单存活但会话已被挪出队列）：拉回队列要让工作台即时刷新。
            _broadcast_after_commit(db, session.id)
        log.info("handoff_reused", ticket_id=str(ticket.id), session_id=str(session.id))
        return ticket

    ticket = HandoffTicket(
        tenant_id=session.tenant_id,
        session_id=session.id,
        channel_type=session.channel_type,
        end_user_id=session.end_user_id,
        reason=reason,
        reason_detail=reason_detail,
        conversation_summary=summary,
        priority="high" if reason in ("negative_feedback", "error_fallback") else "normal",
        status=HandoffStatus.OPEN,
    )
    db.add(ticket)

    session.escalated = True
    # 接管中的会话不降级回「待人工」——人工已经在场，只补一张工单挂上去。
    if session.status != SessionStatus.HUMAN_TAKEOVER:
        session.status = SessionStatus.ESCALATED
    await db.flush()

    # Notify + broadcast only after the ticket is committed: a cancelled/rolled-back turn must
    # not page an operator for a ticket that doesn't exist, and a slow webhook/SMTP must not
    # hold up the customer's reply. The worker records notified / notify_error.
    _notify_after_commit(db, ticket)
    escalations_metric.labels(reason).inc()
    _broadcast_after_commit(db, session.id)

    log.info(
        "handoff_created",
        ticket_id=str(ticket.id),
        session_id=str(session.id),
        reason=reason,
    )
    return ticket


async def resolve_handoff(
    db: AsyncSession, ticket: HandoffTicket, note: str, assignee_id=None
) -> None:
    ticket.status = HandoffStatus.RESOLVED
    ticket.resolution_note = note
    ticket.resolved_at = datetime.now(UTC)
    if assignee_id:
        ticket.assignee_id = assignee_id
    await db.flush()
    # 会话侧回迁：最后一张存活工单解决后，把仍在「待人工」的会话放回 AI 服务。
    # 否则它永远留在待认领计数和列表里，却无单可认领，坐席在收件箱无法处理。
    if not await live_tickets(db, ticket.session_id):
        session = await db.get(Session, ticket.session_id)
        if session and session.status == SessionStatus.ESCALATED:
            session.status = SessionStatus.ACTIVE
            await db.flush()
            await publish_admin_event(
                {"type": "queue", "event": "resolved", "session_id": str(ticket.session_id)}
            )
            # 同时告知客户端：widget 的 escalated 状态只被 ai_resumed/human_ended
            # 事件复位，不通知则「转人工」按钮一直锁着，客户以为还在排队。
            await publish(
                str(ticket.session_id),
                {"type": "ai_resumed", "message": "你的转人工请求已处理，AI 助手继续为你服务。"},
            )


def _notify_after_commit(db: AsyncSession, ticket: HandoffTicket) -> None:
    from app.db.session import run_after_commit
    from app.tasks.queue import enqueue

    tid, tenant = str(ticket.id), str(ticket.tenant_id)
    # job id dedupes repeat escalations of the same ticket while a send is pending.
    run_after_commit(
        db, lambda: enqueue("notify_handoff", tid, _tenant=tenant, _job_id=f"notify-handoff:{tid}")
    )


def _broadcast_after_commit(db: AsyncSession, session_id) -> None:
    from app.db.session import run_after_commit

    sid = str(session_id)
    run_after_commit(
        db, lambda: publish_admin_event({"type": "queue", "event": "handoff", "session_id": sid})
    )


async def send_handoff_notify(
    db: AsyncSession, ticket: HandoffTicket, title: str = "🔔 转人工提醒"
) -> tuple[bool, str]:
    """Deliver the operator notification for a ticket and record the outcome on it."""
    session = await db.get(Session, ticket.session_id)
    who = (session.end_user_display if session else "") or ticket.end_user_id or "匿名"
    body = (
        f"> 渠道：{ticket.channel_type}\n"
        f"> 用户：{who}\n"
        f"> 原因：{ticket.reason_detail or ticket.reason}\n\n"
        f"**对话摘要**：{ticket.conversation_summary[:500]}\n\n"
        f"[点此接待（坐席站）]({agent_console_link(ticket.session_id)})"
        f" ｜ [后台查看]({_admin_console_link(ticket.session_id)})"
    )
    delivered, err = await notify_operator(db, title, body)
    ticket.notified = delivered
    ticket.notify_error = err
    await db.flush()
    log.info("handoff_notify_sent", ticket_id=str(ticket.id), notified=delivered)
    return delivered, err


async def resend_handoff_notify(db: AsyncSession, ticket: HandoffTicket) -> tuple[bool, str]:
    """Re-send the operator notification for an existing ticket (manual retry, synchronous so
    the operator sees the result immediately)."""
    return await send_handoff_notify(db, ticket, title="🔔 转人工提醒（重发）")
