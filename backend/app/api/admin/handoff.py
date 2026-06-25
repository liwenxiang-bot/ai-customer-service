"""Handoff tickets admin: list pending/all, view, resolve."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import HandoffTicket
from app.models.enums import HandoffStatus
from app.services.handoff import resend_handoff_notify, resolve_handoff

router = APIRouter(prefix="/handoff", tags=["admin-handoff"])


@router.get("/tickets")
async def list_tickets(
    status: str = "",
    priority: str = "",
    mine: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    conds = []
    if status:
        conds.append(HandoffTicket.status == status)
    if priority:
        conds.append(HandoffTicket.priority == priority)
    if mine:
        conds.append(HandoffTicket.assignee_id == user.id)

    count_stmt = select(func.count(HandoffTicket.id))
    if conds:
        count_stmt = count_stmt.where(*conds)
    total = (await db.execute(count_stmt)).scalar_one()
    open_count = (
        await db.execute(
            select(func.count(HandoffTicket.id)).where(HandoffTicket.status == HandoffStatus.OPEN)
        )
    ).scalar_one()

    # Urgent/high first, then newest.
    prio_rank = case(
        (HandoffTicket.priority == "urgent", 0),
        (HandoffTicket.priority == "high", 1),
        (HandoffTicket.priority == "low", 3),
        else_=2,
    )
    stmt = select(HandoffTicket, AdminUser.email).outerjoin(
        AdminUser, AdminUser.id == HandoffTicket.assignee_id
    )
    if conds:
        stmt = stmt.where(*conds)
    rows = (
        await db.execute(
            stmt.order_by(prio_rank, HandoffTicket.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "total": total,
        "open_count": open_count,
        "items": [
            {
                "id": str(t.id),
                "session_id": str(t.session_id),
                "channel_type": t.channel_type,
                "end_user_id": t.end_user_id,
                "reason": t.reason,
                "reason_detail": t.reason_detail,
                "conversation_summary": t.conversation_summary,
                "status": t.status,
                "priority": t.priority,
                "assignee_email": email,
                "notified": t.notified,
                "notify_error": t.notify_error,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            }
            for (t, email) in rows
        ],
    }


class ResolveIn(BaseModel):
    note: str = ""


@router.post("/tickets/{ticket_id}/resolve")
async def resolve(
    ticket_id: str, body: ResolveIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    ticket = await db.get(HandoffTicket, uuid.UUID(ticket_id))
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    await resolve_handoff(db, ticket, body.note, user.id)
    await db.commit()
    return {"ok": True}


@router.post("/tickets/{ticket_id}/resend")
async def resend(
    ticket_id: str, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Manually re-send the operator notification for a ticket whose notify failed."""
    ticket = await db.get(HandoffTicket, uuid.UUID(ticket_id))
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    delivered, err = await resend_handoff_notify(db, ticket)
    await db.commit()
    return {"ok": delivered, "notified": delivered, "error": err}


class TicketUpdateIn(BaseModel):
    assignee: str | None = None  # "me" to claim, "" to unassign, None = leave unchanged
    priority: str | None = None  # urgent | high | normal | low


@router.post("/tickets/{ticket_id}/update")
async def update_ticket(
    ticket_id: str, body: TicketUpdateIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Claim/unassign a ticket and/or change its priority."""
    ticket = await db.get(HandoffTicket, uuid.UUID(ticket_id))
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if body.assignee is not None:
        ticket.assignee_id = user.id if body.assignee == "me" else None
    if body.priority in ("urgent", "high", "normal", "low"):
        ticket.priority = body.priority
    await db.commit()
    return {"ok": True}
