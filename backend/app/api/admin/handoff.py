"""Handoff tickets admin: list pending/all, view, resolve."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    stmt = select(HandoffTicket)
    if status:
        stmt = stmt.where(HandoffTicket.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(HandoffTicket.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "open_count": (
            await db.execute(
                select(func.count(HandoffTicket.id)).where(HandoffTicket.status == HandoffStatus.OPEN)
            )
        ).scalar_one(),
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
                "notified": t.notified,
                "notify_error": t.notify_error,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            }
            for t in rows
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
