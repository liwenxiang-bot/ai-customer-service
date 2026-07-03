"""Audit logging for sensitive operations (requirements §9, §11)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin import AuditLog


async def write_audit(
    db: AsyncSession,
    actor,
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict | None = None,
    # None is accepted so callers can pass request.client.host verbatim (it is None
    # for e.g. unix-socket connections); stored as "" — the column is non-nullable.
    ip: str | None = None,
    note: str = "",
) -> None:
    db.add(
        AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", ""),
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
            ip=ip or "",
            note=note,
        )
    )
