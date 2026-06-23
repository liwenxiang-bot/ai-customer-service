"""Conversation records: list with filters, full detail (tool calls + citations + trace),
mark handled, and one-click 'add to knowledge'."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import Message, Session
from app.models.enums import SessionStatus
from app.services import knowledge as ksvc

router = APIRouter(prefix="/conversations", tags=["admin-conversations"])


@router.get("")
async def list_conversations(
    channel_type: str = "",
    escalated: bool | None = None,
    pending_human: bool = False,
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    stmt = select(Session)
    if channel_type:
        stmt = stmt.where(Session.channel_type == channel_type)
    if escalated is not None:
        stmt = stmt.where(Session.escalated.is_(escalated))
    if pending_human:
        stmt = stmt.where(Session.status == SessionStatus.ESCALATED)
    if q:
        stmt = stmt.where(Session.title.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(Session.last_activity_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(s.id),
                "channel_type": s.channel_type,
                "end_user_id": s.end_user_id,
                "end_user_display": s.end_user_display,
                "title": s.title,
                "status": s.status,
                "escalated": s.escalated,
                "message_count": s.message_count,
                "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in rows
        ],
    }


@router.get("/{session_id}")
async def conversation_detail(
    session_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="会话不存在")
    session = await db.get(Session, sid)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = (
        await db.execute(select(Message).where(Message.session_id == sid).order_by(Message.seq.asc()))
    ).scalars().all()
    return {
        "session": {
            "id": str(session.id),
            "channel_type": session.channel_type,
            "end_user_id": session.end_user_id,
            "end_user_display": session.end_user_display,
            "status": session.status,
            "escalated": session.escalated,
            "summary": session.summary,
            "meta": session.meta,
            "created_at": session.created_at.isoformat() if session.created_at else None,
        },
        "messages": [
            {
                "id": str(m.id),
                "seq": m.seq,
                "role": m.role,
                "content": m.content,
                "tool_calls": m.tool_calls,
                "citations": m.citations,
                "trace_id": m.trace_id,
                "model": m.model,
                "prompt_tokens": m.prompt_tokens,
                "completion_tokens": m.completion_tokens,
                "cost_usd": m.cost_usd,
                "latency_ms": m.latency_ms,
                "degraded": m.degraded,
                "feedback": m.feedback,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ],
    }


@router.post("/{session_id}/mark-handled")
async def mark_handled(
    session_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    session = await db.get(Session, uuid.UUID(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.status = SessionStatus.HUMAN_HANDLED
    await db.commit()
    return {"ok": True}


class AddToKnowledgeIn(BaseModel):
    title: str
    content: str
    category: str = ""


@router.post("/{session_id}/to-knowledge")
async def to_knowledge(
    session_id: str, body: AddToKnowledgeIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    item = await ksvc.create_item(
        db, {"title": body.title, "content": body.content, "category": body.category}, user
    )
    await db.commit()
    return {"ok": True, "item_id": str(item.id)}
