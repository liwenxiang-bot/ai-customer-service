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
    ip: str = "",
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
            ip=ip,
            note=note,
        )
    )
