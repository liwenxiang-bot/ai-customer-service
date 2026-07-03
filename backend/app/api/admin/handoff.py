"""Handoff tickets admin: list pending/all, view, claim/unclaim, resolve."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import HandoffTicket
from app.models.enums import AdminRole, HandoffStatus
from app.services.audit import write_audit
from app.services.handoff import resend_handoff_notify, resolve_handoff
from app.services.takeover import publish_admin_event

router = APIRouter(prefix="/handoff", tags=["admin-handoff"])


def _ticket_uuid(ticket_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(ticket_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="工单不存在")


def _ticket_dict(
    t: HandoffTicket, assignee_name: str | None = None, assignee_email: str | None = None
) -> dict:
    return {
        "id": str(t.id),
        "session_id": str(t.session_id),
        "channel_type": t.channel_type,
        "end_user_id": t.end_user_id,
        "reason": t.reason,
        "reason_detail": t.reason_detail,
        "conversation_summary": t.conversation_summary,
        "status": t.status,
        "priority": t.priority,
        "assignee_id": str(t.assignee_id) if t.assignee_id else None,
        "assignee_name": assignee_name,
        "assignee_email": assignee_email,
        "notified": t.notified,
        "notify_error": t.notify_error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
    }


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
    stmt = select(HandoffTicket, AdminUser.name, AdminUser.email).outerjoin(
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
            _ticket_dict(t, assignee_name=(name or email) if email else None, assignee_email=email)
            for (t, name, email) in rows
        ],
    }


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: str, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """坐席抢单：仅 open 且未分配的工单可认领（乐观锁，两个坐席同时抢只有一个成功）。"""
    tid = _ticket_uuid(ticket_id)
    # Atomic grab: the WHERE guard makes concurrent claims race on a single row update,
    # so exactly one wins and the loser sees rowcount 0.
    res = await db.execute(
        update(HandoffTicket)
        .where(
            HandoffTicket.id == tid,
            HandoffTicket.status == HandoffStatus.OPEN,
            HandoffTicket.assignee_id.is_(None),
        )
        .values(assignee_id=user.id, status=HandoffStatus.IN_PROGRESS)
        .execution_options(synchronize_session=False)
    )
    claimed = res.rowcount == 1
    # populate_existing: re-select inside the same transaction so the returned dict
    # reflects the row the UPDATE just wrote (not a stale identity-map copy).
    ticket = await db.get(HandoffTicket, tid, populate_existing=True)
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    my_name = user.name or user.email
    if not claimed:
        # Idempotent: re-claiming your own ticket returns 200 with the same payload.
        if ticket.assignee_id == user.id and ticket.status != HandoffStatus.RESOLVED:
            return _ticket_dict(ticket, assignee_name=my_name, assignee_email=user.email)
        if ticket.assignee_id and ticket.assignee_id != user.id:
            other = await db.get(AdminUser, ticket.assignee_id)
            other_name = (other.name or other.email) if other else "其他坐席"
            raise HTTPException(status_code=409, detail=f"已被 {other_name} 认领")
        raise HTTPException(status_code=409, detail="工单已解决，无法认领")
    await write_audit(
        db, user, "handoff.claim", "handoff_ticket", str(ticket.id),
        {"session_id": str(ticket.session_id)},
    )
    await db.commit()
    await publish_admin_event({
        "type": "queue", "event": "claim",
        "session_id": str(ticket.session_id), "ticket_id": str(ticket.id),
    })
    return _ticket_dict(ticket, assignee_name=my_name, assignee_email=user.email)


@router.post("/tickets/{ticket_id}/unclaim")
async def unclaim_ticket(
    ticket_id: str, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """退回工单：仅本人或管理员可退回，状态回到 open、清空 assignee。"""
    ticket = await db.get(HandoffTicket, _ticket_uuid(ticket_id))
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.status == HandoffStatus.RESOLVED:
        raise HTTPException(status_code=409, detail="工单已解决，无法退回")
    if ticket.assignee_id is None:
        # Already unassigned — idempotent no-op.
        if ticket.status != HandoffStatus.OPEN:
            ticket.status = HandoffStatus.OPEN
            await db.commit()
        return _ticket_dict(ticket)
    if ticket.assignee_id != user.id and user.role != AdminRole.ADMIN:
        raise HTTPException(status_code=403, detail="仅本人或管理员可退回")
    previous_assignee = str(ticket.assignee_id)
    ticket.assignee_id = None
    ticket.status = HandoffStatus.OPEN
    await write_audit(
        db, user, "handoff.unclaim", "handoff_ticket", str(ticket.id),
        {"session_id": str(ticket.session_id), "previous_assignee": previous_assignee},
    )
    await db.commit()
    await publish_admin_event({
        "type": "queue", "event": "unclaim",
        "session_id": str(ticket.session_id), "ticket_id": str(ticket.id),
    })
    return _ticket_dict(ticket)


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
