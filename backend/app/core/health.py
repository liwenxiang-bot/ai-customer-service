"""Dependency liveness probes (DB / Redis / object storage)."""

from __future__ import annotations

import asyncio

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


async def full_health() -> dict:
    db_ok, redis_ok, storage_ok = await asyncio.gather(
        check_db(), check_redis(), check_storage()
    )
    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "dependencies": {
            "database": "up" if db_ok else "down",
            "redis": "up" if redis_ok else "down",
            "object_storage": "up" if storage_ok else "down",
        },
    }
