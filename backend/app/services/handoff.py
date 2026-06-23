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
        status=HandoffStatus.OPEN,
    )
    db.add(ticket)

    session.escalated = True
    session.status = SessionStatus.ESCALATED
    await db.flush()

    # Notify the operator. Best-effort: failure is recorded, not fatal.
    title = "🔔 转人工提醒"
    link = f"{settings.app_base_url}/admin/conversations/{session.id}"
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
