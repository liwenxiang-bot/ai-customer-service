"""Conversation records: list with filters, full detail (tool calls + citations + trace),
mark handled, and one-click 'add to knowledge'."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import Message, Session
from app.models.enums import SessionStatus
from app.services import knowledge as ksvc
from app.services import takeover as tk

router = APIRouter(prefix="/conversations", tags=["admin-conversations"])


def _parse_day(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _filtered_sessions(
    *, channel_type, escalated, pending_human, attention, status, q,
    date_from, date_to, feedback, degraded,
):
    """Build a select(Session) with all conversation filters applied (shared by list + export)."""
    stmt = select(Session)
    if channel_type:
        stmt = stmt.where(Session.channel_type == channel_type)
    if escalated is not None:
        stmt = stmt.where(Session.escalated.is_(escalated))
    if pending_human:
        stmt = stmt.where(Session.status == SessionStatus.ESCALATED)
    if status:
        stmt = stmt.where(Session.status == status)
    # "Needs a human now": awaiting handoff OR currently taken over (workbench queue).
    if attention:
        stmt = stmt.where(
            or_(Session.escalated.is_(True), Session.status == SessionStatus.HUMAN_TAKEOVER)
        )
    if q:
        stmt = stmt.where(Session.title.ilike(f"%{q}%"))
    if (df := _parse_day(date_from)) is not None:
        stmt = stmt.where(Session.created_at >= df)
    if (dt := _parse_day(date_to)) is not None:
        stmt = stmt.where(Session.created_at < dt + timedelta(days=1))  # inclusive end day
    if feedback in ("up", "down"):
        stmt = stmt.where(
            select(Message.id).where(
                Message.session_id == Session.id, Message.feedback == feedback
            ).exists()
        )
    if degraded:
        stmt = stmt.where(
            select(Message.id).where(
                Message.session_id == Session.id, Message.degraded.is_(True)
            ).exists()
        )
    return stmt


@router.get("")
async def list_conversations(
    channel_type: str = "",
    escalated: bool | None = None,
    pending_human: bool = False,
    attention: bool = False,
    status: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    feedback: str = "",
    degraded: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    stmt = _filtered_sessions(
        channel_type=channel_type, escalated=escalated, pending_human=pending_human,
        attention=attention, status=status, q=q, date_from=date_from, date_to=date_to,
        feedback=feedback, degraded=degraded,
    )
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


@router.get("/export")
async def export_conversations(
    channel_type: str = "",
    escalated: bool | None = None,
    pending_human: bool = False,
    attention: bool = False,
    status: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    feedback: str = "",
    degraded: bool = False,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Export matching conversations as CSV (capped at 5000 rows)."""
    stmt = _filtered_sessions(
        channel_type=channel_type, escalated=escalated, pending_human=pending_human,
        attention=attention, status=status, q=q, date_from=date_from, date_to=date_to,
        feedback=feedback, degraded=degraded,
    )
    rows = (
        await db.execute(stmt.order_by(Session.last_activity_at.desc()).limit(5000))
    ).scalars().all()

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly
    w = csv.writer(buf)
    w.writerow(["会话ID", "渠道", "用户", "标题", "状态", "是否转人工", "轮数", "创建时间", "最近活动"])
    for s in rows:
        w.writerow([
            str(s.id), s.channel_type, s.end_user_display or s.end_user_id, s.title, s.status,
            "是" if s.escalated else "否", s.message_count,
            s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else "",
            s.last_activity_at.strftime("%Y-%m-%d %H:%M") if s.last_activity_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=conversations.csv"},
    )


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
            "satisfaction_rating": session.satisfaction_rating,
            "satisfaction_note": session.satisfaction_note,
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
                "attachments": m.attachments,
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


# ----------------------------------------------------------- live human takeover
async def _get_session_or_404(db: AsyncSession, session_id: str) -> Session:
    try:
        s = await db.get(Session, uuid.UUID(session_id))
    except (ValueError, TypeError):
        s = None
    if not s:
        raise HTTPException(status_code=404, detail="会话不存在")
    return s


@router.post("/{session_id}/takeover")
async def takeover(
    session_id: str, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Operator takes over: AI pauses, operator chats live with the customer."""
    session = await _get_session_or_404(db, session_id)
    await tk.start_takeover(db, session)
    await db.commit()
    return {"ok": True, "status": session.status}


class ReplyIn(BaseModel):
    content: str


@router.post("/{session_id}/reply")
async def reply(
    session_id: str, body: ReplyIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Operator sends a message; it is pushed live to the customer's chat window."""
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    session = await _get_session_or_404(db, session_id)
    msg = await tk.operator_reply(db, session, content)
    await db.commit()
    return {"ok": True, "message_id": str(msg.id)}


class ReleaseIn(BaseModel):
    resume_ai: bool = True


@router.post("/{session_id}/release")
async def release(
    session_id: str, body: ReleaseIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """End takeover. resume_ai=True → AI resumes; False → mark session human-handled."""
    session = await _get_session_or_404(db, session_id)
    await tk.end_takeover(db, session, resume_ai=body.resume_ai)
    await db.commit()
    return {"ok": True, "status": session.status}
