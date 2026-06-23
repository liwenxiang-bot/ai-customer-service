"""Data retention cleanup (PIPL): purge conversations past the retention window.

Cascades delete messages/handoff tickets via FK. Runs on a schedule from the worker
(requirements §11, §13). retention_days <= 0 disables it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.models.conversation import Session

log = get_logger("cleanup")


async def cleanup_expired(db: AsyncSession) -> int:
    if settings.data_retention_days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=settings.data_retention_days)
    ids = (
        await db.execute(select(Session.id).where(Session.last_activity_at < cutoff))
    ).scalars().all()
    if not ids:
        return 0
    await db.execute(delete(Session).where(Session.id.in_(ids)))
    await db.commit()
    log.info("cleanup_done", deleted_sessions=len(ids), cutoff=cutoff.isoformat())
    return len(ids)


async def delete_user_data(db: AsyncSession, end_user_id: str) -> int:
    """Honor a user deletion request (PIPL)."""
    ids = (
        await db.execute(select(Session.id).where(Session.end_user_id == end_user_id))
    ).scalars().all()
    if ids:
        await db.execute(delete(Session).where(Session.id.in_(ids)))
        await db.commit()
    return len(ids)
