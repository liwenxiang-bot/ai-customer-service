"""Dashboard metrics: today's conversations/users, response time, escalation & CSAT, cost."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.clock import app_day_start_utc, app_today
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
    today_start = app_day_start_utc(app_today())

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
    start = app_today() - timedelta(days=days - 1)
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


@router.get("/analytics")
async def analytics(days: int = 14, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    """Operational analytics over a window: CSAT (1–5), knowledge grounding + most-cited
    items (from message citations), and cost broken down by model and channel."""
    days = max(1, min(days, 90))
    start = app_day_start_utc(app_today() - timedelta(days=days - 1))

    # ---- CSAT: visitor's 1–5 star rating ----
    rating_rows = (
        await db.execute(
            select(Session.satisfaction_rating, func.count(Session.id))
            .where(Session.satisfaction_rating.is_not(None), Session.created_at >= start)
            .group_by(Session.satisfaction_rating)
        )
    ).all()
    dist = {int(r[0]): int(r[1]) for r in rating_rows}
    csat_distribution = [{"rating": s, "count": dist.get(s, 0)} for s in (1, 2, 3, 4, 5)]
    rated = sum(d["count"] for d in csat_distribution)
    csat_avg = (
        round(sum(d["rating"] * d["count"] for d in csat_distribution) / rated, 2) if rated else None
    )

    # ---- Knowledge grounding rate: assistant answers that cited the KB ----
    grounded, total_answers = (
        await db.execute(
            select(
                func.count(Message.id).filter(func.jsonb_array_length(Message.citations) > 0),
                func.count(Message.id),
            ).where(Message.role == MessageRole.ASSISTANT, Message.created_at >= start)
        )
    ).one()
    grounded, total_answers = int(grounded or 0), int(total_answers or 0)
    grounding_rate = round(grounded / total_answers, 3) if total_answers else None

    # ---- Most-cited knowledge items (unnest the citations JSONB array) ----
    top_rows = (
        await db.execute(
            text(
                """
                SELECT c->>'item_id' AS item_id,
                       max(c->>'title') AS title,
                       count(*) AS hits,
                       avg((c->>'score')::float) AS avg_score
                FROM messages m, jsonb_array_elements(m.citations) c
                WHERE m.created_at >= :start AND jsonb_array_length(m.citations) > 0
                GROUP BY c->>'item_id'
                ORDER BY hits DESC
                LIMIT 10
                """
            ),
            {"start": start},
        )
    ).all()
    top_items = [
        {
            "item_id": r[0],
            "title": r[1] or "(无标题)",
            "hits": int(r[2]),
            "avg_score": round(float(r[3] or 0), 3),
        }
        for r in top_rows
    ]

    # ---- Cost breakdown ----
    by_model = (
        await db.execute(
            select(Message.model, func.sum(Message.cost_usd), func.count(Message.id))
            .where(Message.created_at >= start, Message.cost_usd > 0)
            .group_by(Message.model)
            .order_by(func.sum(Message.cost_usd).desc())
        )
    ).all()
    cost_by_model = [
        {"model": r[0] or "(unknown)", "cost_usd": round(float(r[1] or 0), 4), "messages": int(r[2])}
        for r in by_model
    ]
    by_channel = (
        await db.execute(
            select(Session.channel_type, func.sum(Message.cost_usd))
            .join(Message, Message.session_id == Session.id)
            .where(Message.created_at >= start, Message.cost_usd > 0)
            .group_by(Session.channel_type)
            .order_by(func.sum(Message.cost_usd).desc())
        )
    ).all()
    cost_by_channel = [
        {"channel": r[0] or "(unknown)", "cost_usd": round(float(r[1] or 0), 4)} for r in by_channel
    ]

    return {
        "days": days,
        "csat": {"distribution": csat_distribution, "average": csat_avg, "rated": rated},
        "knowledge": {
            "grounding_rate": grounding_rate,
            "grounded": grounded,
            "total_answers": total_answers,
            "top_items": top_items,
        },
        "cost": {"by_model": cost_by_model, "by_channel": cost_by_channel},
    }
