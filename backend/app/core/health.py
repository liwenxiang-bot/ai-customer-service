"""Dependency liveness probes (DB / Redis / object storage)."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import text

from app.config import settings
from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.core.storage import _client
from app.db.session import engine

log = get_logger("health")


async def check_db() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("health_db_down", error=str(exc))
        return False


async def check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("health_redis_down", error=str(exc))
        return False


async def check_storage() -> bool:
    try:
        await asyncio.to_thread(_client().head_bucket, Bucket=settings.minio_bucket)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("health_storage_down", error=str(exc))
        return False


async def check_embedding_dim() -> bool:
    """Embedding dimension must agree in three places: env EMBEDDING_DIM (declares the ORM
    column), the active AIConfig row (drives runtime embed calls), and the actual
    knowledge_chunks.embedding column typmod (what the DB enforces). A mismatch means
    embedding writes fail at commit and vector search returns nothing — visible only in
    worker logs until now, so surface it as a readiness failure."""
    try:
        async with engine.connect() as conn:
            # pgvector stores the dimension directly in atttypmod (-1 = unconstrained).
            col_dim = (
                await conn.execute(
                    text(
                        "SELECT atttypmod FROM pg_attribute "
                        "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'"
                    )
                )
            ).scalar_one_or_none()
            # ai_configs is per-tenant (one active row each) while the vector column is
            # global — every tenant's dim has to match it, so compare against all of them.
            cfg_dims = (
                await conn.execute(
                    text("SELECT DISTINCT embedding_dim FROM ai_configs WHERE is_active IS TRUE")
                )
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 — DB down / pre-migration: check_db owns that signal
        log.warning("health_embedding_dim_unknown", error=str(exc))
        return True
    observed = {
        "env": settings.embedding_dim,
        # No active config yet (first boot) / unconstrained column → nothing to compare.
        "ai_config": sorted(cfg_dims) or None,
        "db_column": col_dim if (col_dim or 0) > 0 else None,
    }
    dims = {settings.embedding_dim, *cfg_dims}
    if (col_dim or 0) > 0:
        dims.add(col_dim)
    if len(dims) > 1:
        log.critical(
            "embedding_dim_mismatch",
            **observed,
            hint="align EMBEDDING_DIM, the active ai_config and the vector column "
            "(admin dim change triggers ALTER + rebuild), then re-embed",
        )
        return False
    return True


_HEALTH_TTL = 5.0
_health_cache: tuple[float, dict] | None = None


async def full_health() -> dict:
    """Dependency health, cached for a few seconds: /health is reachable from the internet and
    each call probes DB, Redis, object storage and the vector column — unthrottled, it's a
    cheap way to load every dependency at once."""
    global _health_cache
    now = time.monotonic()
    if _health_cache and now - _health_cache[0] < _HEALTH_TTL:
        return _health_cache[1]
    result = await _probe()
    _health_cache = (now, result)
    return result


async def _probe() -> dict:
    db_ok, redis_ok, storage_ok, dim_ok = await asyncio.gather(
        check_db(), check_redis(), check_storage(), check_embedding_dim()
    )
    return {
        "status": "ok" if (db_ok and redis_ok and dim_ok) else "degraded",
        "dependencies": {
            "database": "up" if db_ok else "down",
            "redis": "up" if redis_ok else "down",
            "object_storage": "up" if storage_ok else "down",
            "embedding_dim": "consistent" if dim_ok else "mismatch",
        },
    }
