"""Async database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
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
