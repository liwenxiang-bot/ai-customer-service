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
from app.models.conversation import Message, Session

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
    keys = await _attachment_keys(db, ids)
    await db.execute(delete(Session).where(Session.id.in_(ids)))
    await db.commit()
    files = await _delete_files(keys)
    log.info("cleanup_done", deleted_sessions=len(ids), deleted_files=files, cutoff=cutoff.isoformat())
    return len(ids)


async def delete_user_data(db: AsyncSession, end_user_id: str) -> int:
    """Honor a user deletion request (PIPL)."""
    ids = (
        await db.execute(select(Session.id).where(Session.end_user_id == end_user_id))
    ).scalars().all()
    if ids:
        keys = await _attachment_keys(db, ids)
        await db.execute(delete(Session).where(Session.id.in_(ids)))
        await db.commit()
        await _delete_files(keys)
    return len(ids)


async def _attachment_keys(db: AsyncSession, session_ids: list) -> list[str]:
    """Object-storage keys of every attachment in these sessions (customer AND operator
    uploads) — the DB cascade removes the rows, not the files (PIPL: the files are personal
    data too)."""
    rows = (
        await db.execute(select(Message.attachments).where(Message.session_id.in_(session_ids)))
    ).scalars().all()
    return [a["key"] for atts in rows for a in (atts or []) if isinstance(a, dict) and a.get("key")]


async def _delete_files(keys: list[str]) -> int:
    if not keys:
        return 0
    from app.core import storage

    try:
        return await storage.delete_objects(keys)
    except Exception as exc:  # noqa: BLE001 — DB purge already done; report, retry next run
        log.warning("cleanup_files_failed", count=len(keys), error=str(exc))
        return 0
