"""Async database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import text
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

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def _pin_tenant(session: AsyncSession) -> None:
    """Pin Postgres' app.tenant_id for this transaction so RLS filters to the active tenant.
    Empty when no context is set → RLS matches nothing (fail-closed). Uses set_config(...,
    is_local=true), i.e. transaction-scoped, so it never leaks across pooled connections.

    Note: the codebase commits terminally within a session_scope/request, so one pin per
    session is sufficient. A path that commits and then keeps querying the same session must
    re-pin (or open a new session)."""
    tid = get_current_tenant()
    await session.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"),
        {"t": str(tid) if tid else ""},
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped, tenant-pinned session."""
    async with SessionLocal() as session:
        await _pin_tenant(session)
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
        await _pin_tenant(self._session)
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
