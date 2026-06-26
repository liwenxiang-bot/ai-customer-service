"""Per-request / per-task tenant context — the source of truth for RLS isolation.

Set the active tenant (``set_current_tenant`` / ``tenant_scope``); every DB session opened
afterwards pins Postgres' ``app.tenant_id`` GUC (see ``db.session``), so RLS policies filter
every query to that tenant, and new rows default their ``tenant_id`` to it (see
``db.base.TenantMixin``).

Fail-closed: with no context set, the GUC is empty → RLS sees NULL → **zero rows** and
inserts are rejected. So every entry point (request, WS, worker job, script) MUST establish
a tenant first. System/setup paths use ``tenant_scope(DEFAULT_TENANT_ID)`` explicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.config import settings

DEFAULT_TENANT_ID = uuid.UUID(settings.default_tenant_id)

_current: ContextVar[uuid.UUID | None] = ContextVar("acs_current_tenant", default=None)


def _coerce(tid: object) -> uuid.UUID | None:
    if tid is None:
        return None
    return tid if isinstance(tid, uuid.UUID) else uuid.UUID(str(tid))


def set_current_tenant(tid: object) -> None:
    _current.set(_coerce(tid))


def get_current_tenant() -> uuid.UUID | None:
    return _current.get()


def current_tenant_for_insert() -> uuid.UUID:
    """ORM column default — new rows belong to the active tenant. Falls back to the default
    tenant only for system/bootstrap paths that intentionally run under it."""
    return _current.get() or DEFAULT_TENANT_ID


@contextmanager
def tenant_scope(tid: object) -> Iterator[None]:
    """Bind a tenant for the duration of the block (workers, scripts, system tasks)."""
    token = _current.set(_coerce(tid))
    try:
        yield
    finally:
        _current.reset(token)
