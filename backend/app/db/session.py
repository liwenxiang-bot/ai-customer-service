"""Async database engine and session management."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncGenerator, Awaitable, Callable

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.core.logging import get_logger
from app.db.tenant_context import get_current_tenant

engine = create_async_engine(
    # Runtime connection: the non-superuser app role when configured, so RLS is enforced.
    settings.app_sqlalchemy_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)


@event.listens_for(engine.sync_engine, "begin")
def _pin_tenant_on_begin(conn) -> None:
    """Pin Postgres' app.tenant_id at the start of EVERY transaction (not once per session),
    so RLS keeps filtering to the active tenant even after a mid-session commit opens a new
    transaction (e.g. the rebuild job's progress commits). set_config(is_local=true) →
    transaction-scoped, never leaks across the pool; empty when no tenant context → RLS
    matches nothing (fail-closed).

    The value is always a UUID string or '' (the contextvar coerces to uuid.UUID), so direct
    interpolation is injection-safe. Listening on sync_engine + exec_driver_sql is the
    documented way to run per-transaction SQL under the asyncio extension.
    """
    tid = get_current_tenant()
    val = str(tid) if tid else ""
    conn.exec_driver_sql(f"SELECT set_config('app.tenant_id', '{val}', true)")


SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session (tenant pinned per transaction)."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


class session_scope:
    """Async context manager for use outside request handlers (tasks, scripts).

    The caller must have established a tenant (db.tenant_context.tenant_scope / set_current_
    tenant) first, or RLS will return nothing."""

    def __init__(self) -> None:
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = SessionLocal()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._session is not None
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()


# ---- Post-commit side effects ----
# External side effects (enqueue a notification, push to WeChat, broadcast to the workbench)
# must only happen once the data they describe is durable: firing them inside the transaction
# means a rollback/cancel leaves operators chasing a ticket or message that doesn't exist.
# `run_after_commit` queues a coroutine factory on the session; it runs (fire-and-forget) right
# after the next successful COMMIT and is dropped on ROLLBACK.

_AFTER_COMMIT_KEY = "acs_after_commit"
_background: set[asyncio.Task] = set()
_log = get_logger("db.session")


def run_after_commit(db: AsyncSession, fn: Callable[[], Awaitable[object]]) -> None:
    db.sync_session.info.setdefault(_AFTER_COMMIT_KEY, []).append(fn)


async def _run_safely(fn: Callable[[], Awaitable[object]]) -> None:
    try:
        await fn()
    except Exception as exc:  # noqa: BLE001 — a side effect must never surface as a request error
        _log.warning("after_commit_failed", error=str(exc))


@event.listens_for(OrmSession, "after_commit")
def _fire_after_commit(sync_session) -> None:
    fns = sync_session.info.pop(_AFTER_COMMIT_KEY, None)
    if not fns:
        return
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()  # keep the tenant / trace context of the committing code
    for fn in fns:
        task = loop.create_task(_run_safely(fn), context=ctx)
        _background.add(task)
        task.add_done_callback(_background.discard)


@event.listens_for(OrmSession, "after_rollback")
def _drop_after_commit(sync_session) -> None:
    sync_session.info.pop(_AFTER_COMMIT_KEY, None)


async def drain_after_commit_tasks() -> None:
    """Await in-flight post-commit side effects (tests, graceful shutdown)."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)
