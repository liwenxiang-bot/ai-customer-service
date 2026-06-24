"""Dashboard metrics: today's conversations/users, response time, escalation & CSAT, cost."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import Message, Session
from app.models.enums import FeedbackKind, MessageRole, SessionStatus
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.models.usage import UsageDaily
from app.services.usage import get_today_cost

router = APIRouter(prefix="/dashboard", tags=["admin-dashboard"])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    now = datetime.now(UTC)
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)

    today_convs = (
        await db.execute(select(func.count(Session.id)).where(Session.created_at >= today_start))
    ).scalar_one()
    today_users = (
        await db.execute(
            select(func.count(func.distinct(Session.end_user_id))).where(Session.created_at >= today_start)
        )
    ).scalar_one()
    today_msgs = (
        await db.execute(
            select(func.count(Message.id)).where(
                Message.created_at >= today_start, Message.role == MessageRole.USER
            )
        )
    ).scalar_one()

    # Average assistant latency (today).
    avg_latency = (
        await db.execute(
            select(func.avg(Message.latency_ms)).where(
                Message.created_at >= today_start, Message.role == MessageRole.ASSISTANT, Message.latency_ms > 0
            )
        )
    ).scalar_one() or 0

    # Escalation rate (today).
    escalated = (
        await db.execute(
            select(func.count(Session.id)).where(Session.created_at >= today_start, Session.escalated.is_(True))
        )
    ).scalar_one()
    escalation_rate = round(escalated / today_convs, 3) if today_convs else 0.0

    # Satisfaction (👍 / total rated), all-time recent.
    up = (
        await db.execute(select(func.count(Message.id)).where(Message.feedback == FeedbackKind.UP))
    ).scalar_one()
    down = (
        await db.execute(select(func.count(Message.id)).where(Message.feedback == FeedbackKind.DOWN))
    ).scalar_one()
    satisfaction = round(up / (up + down), 3) if (up + down) else None

    today_cost = await get_today_cost()

    # Operational backlog + knowledge health (for the dashboard panels / quick links).
    pending_human = (
        await db.execute(
            select(func.count(Session.id)).where(Session.status == SessionStatus.ESCALATED)
        )
    ).scalar_one()
    in_takeover = (
        await db.execute(
            select(func.count(Session.id)).where(Session.status == SessionStatus.HUMAN_TAKEOVER)
        )
    ).scalar_one()
    knowledge_items = (await db.execute(select(func.count(KnowledgeItem.id)))).scalar_one()
    chunks_total = (await db.execute(select(func.count(KnowledgeChunk.id)))).scalar_one()
    chunks_ready = (
        await db.execute(select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.status == "ready"))
    ).scalar_one()

    return {
        "today": {
            "conversations": today_convs,
            "users": today_users,
            "user_messages": today_msgs,
            "avg_latency_ms": int(avg_latency),
            "escalation_rate": escalation_rate,
            "cost_usd": round(today_cost, 4),
        },
        "satisfaction": satisfaction,
        "feedback": {"up": up, "down": down},
        "backlog": {"pending_human": pending_human, "in_takeover": in_takeover},
        "knowledge": {
            "items": knowledge_items,
            "chunks_total": chunks_total,
            "chunks_ready": chunks_ready,
        },
    }


@router.get("/trend")
async def trend(days: int = 14, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    start = date.today() - timedelta(days=days - 1)
    rows = (
        await db.execute(
            select(
                UsageDaily.day,
                func.sum(UsageDaily.conversations),
                func.sum(UsageDaily.messages),
                func.sum(UsageDaily.cost_usd),
                func.sum(UsageDaily.escalations),
            )
            .where(UsageDaily.day >= start)
            .group_by(UsageDaily.day)
            .order_by(UsageDaily.day)
        )
    ).all()
    return {
        "trend": [
            {
                "day": r[0].isoformat(),
                "conversations": int(r[1] or 0),
                "messages": int(r[2] or 0),
                "cost_usd": round(float(r[3] or 0), 4),
                "escalations": int(r[4] or 0),
            }
            for r in rows
        ]
    }
