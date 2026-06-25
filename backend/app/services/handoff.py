"""Human handoff: create a ticket, notify the operator, mark the session.

Lightweight by design (requirements §8): notify + record + tell the customer. The
ticket schema is rich enough to grow into a full agent workbench later without a
rewrite.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.core.metrics import escalations as escalations_metric
from app.models.conversation import HandoffTicket, Session
from app.models.enums import HandoffStatus, SessionStatus
from app.services.notify import notify_operator
from app.services.takeover import publish_admin_event

log = get_logger("handoff")


async def create_handoff(
    db: AsyncSession,
    session: Session,
    reason: str,
    reason_detail: str,
    summary: str,
) -> HandoffTicket:
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
    session.status = SessionStatus.ESCALATED
    await db.flush()

    # Notify the operator. Best-effort: failure is recorded, not fatal.
    title = "🔔 转人工提醒"
    base = (settings.admin_base_url or settings.app_base_url).rstrip("/")
    link = f"{base}/conversations?session={session.id}"
    body = (
        f"> 渠道：{session.channel_type}\n"
        f"> 用户：{session.end_user_display or session.end_user_id or '匿名'}\n"
        f"> 原因：{reason_detail or reason}\n\n"
        f"**对话摘要**：{summary[:500]}\n\n"
        f"[点此在后台查看]({link})"
    )
    delivered, err = await notify_operator(db, title, body)
    ticket.notified = delivered
    ticket.notify_error = err
    await db.flush()
    escalations_metric.labels(reason).inc()
    await publish_admin_event({"type": "queue", "event": "handoff", "session_id": str(session.id)})

    log.info(
        "handoff_created",
        ticket_id=str(ticket.id),
        session_id=str(session.id),
        reason=reason,
        notified=delivered,
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


async def resend_handoff_notify(db: AsyncSession, ticket: HandoffTicket) -> tuple[bool, str]:
    """Re-send the operator notification for an existing ticket (manual retry)."""
    base = (settings.admin_base_url or settings.app_base_url).rstrip("/")
    link = f"{base}/conversations?session={ticket.session_id}"
    title = "🔔 转人工提醒（重发）"
    body = (
        f"> 渠道：{ticket.channel_type}\n"
        f"> 用户：{ticket.end_user_id or '匿名'}\n"
        f"> 原因：{ticket.reason_detail or ticket.reason}\n\n"
        f"**对话摘要**：{ticket.conversation_summary[:500]}\n\n"
        f"[点此在后台查看]({link})"
    )
    delivered, err = await notify_operator(db, title, body)
    ticket.notified = delivered
    ticket.notify_error = err
    await db.flush()
    log.info("handoff_notify_resent", ticket_id=str(ticket.id), notified=delivered)
    return delivered, err
