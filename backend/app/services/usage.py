"""Cost & usage accounting + the daily cost circuit breaker.

Live counter lives in Redis (atomic INCRBYFLOAT, fast path on every turn); the durable
daily rollup lives in usage_daily for the dashboard. When the day's spend crosses the
configured cap, the public chat entry degrades with a friendly notice and alerts
(requirements §5 防滥用与成本管控).
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.clock import app_today
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.llm.types import Usage
from app.models.conversation import Session
from app.models.usage import UsageDaily

log = get_logger("usage")


def _today_key() -> str:
    return f"cost:daily:{app_today():%Y-%m-%d}"


async def get_today_cost() -> float:
    val = await get_redis().get(_today_key())
    try:
        return float(val) if val else 0.0
    except (TypeError, ValueError):
        return 0.0


async def is_cost_capped() -> bool:
    cap = settings.daily_cost_cap_usd
    if cap <= 0:
        return False
    return await get_today_cost() >= cap


async def add_cost(cost: float) -> float:
    """Atomically add to today's spend; return the new total. Key expires after 48h."""
    r = get_redis()
    key = _today_key()
    total = await r.incrbyfloat(key, cost)
    await r.expire(key, 60 * 60 * 48)
    cap = settings.daily_cost_cap_usd
    if cap > 0 and total >= cap > (total - cost):
        # First crossing of the threshold → alert once.
        log.warning("daily_cost_cap_reached", total=round(total, 4), cap=cap)
    return total


async def record_turn_cost(
    db: AsyncSession, session: Session, usage: Usage, cost: float
) -> None:
    await add_cost(cost)
    today = app_today()
    stmt = (
        pg_insert(UsageDaily)
        .values(
            tenant_id=session.tenant_id,
            day=today,
            channel_type=session.channel_type,
            conversations=0,
            messages=1,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=cost,
            escalations=0,
        )
        .on_conflict_do_update(
            constraint="uq_usage_day_channel",
            set_={
                "messages": UsageDaily.messages + 1,
                "prompt_tokens": UsageDaily.prompt_tokens + usage.prompt_tokens,
                "completion_tokens": UsageDaily.completion_tokens + usage.completion_tokens,
                "cost_usd": UsageDaily.cost_usd + cost,
            },
        )
    )
    await db.execute(stmt)


async def mark_new_conversation(db: AsyncSession, session: Session) -> None:
    today = app_today()
    stmt = (
        pg_insert(UsageDaily)
        .values(
            tenant_id=session.tenant_id,
            day=today,
            channel_type=session.channel_type,
            conversations=1,
        )
        .on_conflict_do_update(
            constraint="uq_usage_day_channel",
            set_={"conversations": UsageDaily.conversations + 1},
        )
    )
    await db.execute(stmt)


async def mark_escalation(db: AsyncSession, session: Session) -> None:
    today = app_today()
    stmt = (
        pg_insert(UsageDaily)
        .values(
            tenant_id=session.tenant_id,
            day=today,
            channel_type=session.channel_type,
            escalations=1,
        )
        .on_conflict_do_update(
            constraint="uq_usage_day_channel",
            set_={"escalations": UsageDaily.escalations + 1},
        )
    )
    await db.execute(stmt)
