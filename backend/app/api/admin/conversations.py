"""Conversation records: list with filters, full detail (tool calls + citations + trace),
mark handled, and one-click 'add to knowledge'."""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.core.strings import escape_like
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import HandoffTicket, Message, Session
from app.models.enums import HandoffStatus, SessionStatus
from app.services import knowledge as ksvc
from app.services import takeover as tk
from app.services.audit import write_audit
from app.services.handoff import QUEUE_STATUSES, resolve_session_tickets

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
        # Workbench queue = waiting (escalated) or being handled (takeover). Status-based so
        # resolved / AI-resumed sessions (where the `escalated` flag lingers True) don't leak in.
        stmt = stmt.where(Session.status.in_(QUEUE_STATUSES))
    if q:
        like = f"%{escape_like(q)}%"
        stmt = stmt.where(
            or_(
                Session.title.ilike(like, escape="\\"),
                Session.end_user_display.ilike(like, escape="\\"),
                select(Message.id)
                .where(Message.session_id == Session.id, Message.content.ilike(like, escape="\\"))
                .exists(),
            )
        )
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

    # Queue views (转人工待接待/需要人工) also need the ticket + claimer, so agents can
    # see who grabbed what: latest non-resolved ticket per session, or null.
    tickets_by_session: dict = {}
    if (pending_human or attention) and rows:
        trows = (
            await db.execute(
                select(HandoffTicket, AdminUser.name, AdminUser.email)
                .outerjoin(AdminUser, AdminUser.id == HandoffTicket.assignee_id)
                .where(
                    HandoffTicket.session_id.in_([s.id for s in rows]),
                    HandoffTicket.status != HandoffStatus.RESOLVED,
                )
                .order_by(HandoffTicket.session_id, HandoffTicket.created_at.desc())
            )
        ).all()
        for t, name, email in trows:
            if t.session_id not in tickets_by_session:  # first = newest per session
                tickets_by_session[t.session_id] = {
                    "id": str(t.id),
                    "status": t.status,
                    "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                    "assignee_name": (name or email) if t.assignee_id and email else None,
                }

    items = []
    for s in rows:
        item = {
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
        if pending_human or attention:
            item["ticket"] = tickets_by_session.get(s.id)
            # 坐席站「我的」tab 用它把"未认领即接管"的会话纳入列表，与角标计数口径一致。
            item["takeover_by_id"] = str(s.takeover_by_id) if s.takeover_by_id else None
        items.append(item)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


@router.get("/queue-counts")
async def queue_counts(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    """Counts for the workbench tabs: waiting (escalated), in-takeover, and mine
    (工单认领给我的 + 我接管中的会话，按会话去重)."""
    # 已被认领（in_progress 工单）的会话不再算「待认领」——它已归属某个坐席，
    # 应只出现在其「我的」里。否则坐席认领后该会话仍留在待认领队列、计数不变，令人困惑。
    claimed_sessions = select(HandoffTicket.session_id).where(
        HandoffTicket.status == HandoffStatus.IN_PROGRESS
    )
    escalated_count = func.count(Session.id)
    waiting = (
        await db.execute(
            select(escalated_count).where(
                Session.status == SessionStatus.ESCALATED,
                Session.id.notin_(claimed_sessions),
            )
        )
    ).scalar_one()
    escalated_total = (
        await db.execute(
            select(func.count(Session.id)).where(Session.status == SessionStatus.ESCALATED)
        )
    ).scalar_one()
    takeover = (
        await db.execute(
            select(func.count(Session.id)).where(Session.status == SessionStatus.HUMAN_TAKEOVER)
        )
    ).scalar_one()
    # 工单半边 join 会话在队状态，与「我的」列表口径（attention 基集）严格对齐：
    # 工单若因异常路径滞留在已离队会话上，不再把角标撑成"计数 2、列表空"的幽灵。
    my_ticket_sessions = (
        select(HandoffTicket.session_id.label("sid"))
        .join(Session, Session.id == HandoffTicket.session_id)
        .where(
            HandoffTicket.assignee_id == user.id,
            HandoffTicket.status == HandoffStatus.IN_PROGRESS,
            Session.status.in_(QUEUE_STATUSES),
        )
    )
    my_takeover_sessions = select(Session.id.label("sid")).where(
        Session.status == SessionStatus.HUMAN_TAKEOVER,
        Session.takeover_by_id == user.id,
    )
    mine = (
        await db.execute(
            select(func.count()).select_from(
                union(my_ticket_sessions, my_takeover_sessions).subquery()
            )
        )
    ).scalar_one()
    # 「全部」= 需要人工的完整队列（所有 escalated + 所有 takeover），是待认领的超集。
    return {"waiting": waiting, "mine": mine, "takeover": takeover, "all": escalated_total + takeover}


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
    was_takeover = session.status == SessionStatus.HUMAN_TAKEOVER
    session.status = SessionStatus.HUMAN_HANDLED
    # 会话离队必须收尾工单，否则已认领的 in_progress 工单滞留成幽灵计数
    # （只进 queue-counts 的 mine 角标、不进任何列表）。
    session.takeover_by_id = None
    session.takeover_by_name = ""
    resolved = await resolve_session_tickets(db, session, "标记已处理", assignee_id=user.id)
    if resolved:
        await write_audit(
            db, user, "handoff.auto_resolve", "session", session_id,
            {"tickets": resolved, "note": "标记已处理"},
        )
    await db.commit()
    if was_takeover:
        # 接管态残留也要清 Redis 标志：is_takeover 只看标志，不清则 AI 对该会话
        # 哑火最长 24h（客户消息只落库、无人应答，且会话已不在任何队列里）。
        await tk.clear_takeover_flag(session_id)
    await tk.publish_admin_event({"type": "queue", "event": "handled", "session_id": session_id})
    return {"ok": True}


class AddToKnowledgeIn(BaseModel):
    title: str
    content: str
    category: str = ""
    message_id: str = ""


@router.post("/{session_id}/to-knowledge")
async def to_knowledge(
    session_id: str, body: AddToKnowledgeIn, request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    session = await _get_session_or_404(db, session_id)
    # 内容查重：同一条高频好答案常被不同管理员从不同会话各点一次「加入知识库」，
    # 不拦会静默产生近重复条目挤占 top_k。规范化口径与 import_knowledge 的
    # _content_fingerprint / review_approve 的查重 SQL 一致（去全部空白后比对）。
    norm_content = re.sub(r"\s+", "", body.content)
    dup = (
        await db.execute(
            text(
                "SELECT id::text, title FROM knowledge_items "
                "WHERE status <> 'archived' "
                "  AND :norm <> '' AND regexp_replace(content, '\\s+', '', 'g') = :norm "
                "LIMIT 1"
            ),
            {"norm": norm_content},
        )
    ).first()
    if dup:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"知识库已有相同内容的条目：{dup[1] or dup[0]}，请直接编辑该条目，或修改内容后再添加",
                "item_id": dup[0],
                "title": dup[1],
            },
        )
    item = await ksvc.create_item(
        db, {"title": body.title, "content": body.content, "category": body.category}, user
    )
    # 来源回溯：记下会话/消息 id，事后能核实「这条答案当时的上下文」。
    provenance = {"session_id": str(session.id)}
    if body.message_id:
        provenance["message_id"] = body.message_id
    item.extra = provenance
    await write_audit(
        db, user, "conversation.to_knowledge", "knowledge_item", str(item.id),
        {"title": item.title, **provenance},
        ip=request.client.host if request.client else "",
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
    await tk.start_takeover(db, session, operator=user)
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
    """End takeover. resume_ai=True → AI resumes; False → mark session human-handled.
    两个分支都会自动 resolve 该会话的 open/in_progress 工单（人工介入已结束）。"""
    session = await _get_session_or_404(db, session_id)
    resolved = await tk.end_takeover(db, session, resume_ai=body.resume_ai, operator=user)
    if resolved:
        await write_audit(
            db, user, "handoff.auto_resolve", "session", session_id,
            # 与工单 resolution_note 同口径，否则审计会把「AI 接续」误读成关闭会话。
            {"tickets": resolved,
             "note": "坐席结束接管（AI 接续）" if body.resume_ai else "坐席结束会话"},
        )
    await db.commit()
    return {"ok": True, "status": session.status, "resolved_tickets": resolved}
