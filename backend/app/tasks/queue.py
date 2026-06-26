"""ARQ enqueue helper. A single shared pool; enqueue is best-effort.

Heavy work (embedding generation, full vector rebuild, batch import, summarization,
knowledge distillation, data cleanup) is pushed here so request/turn latency stays low
(requirements §13).
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings
from app.core.logging import get_logger
from app.db.tenant_context import get_current_tenant

log = get_logger("tasks.queue")

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings(
        host=settings.redis_host,
        port=settings.redis_port,
        database=settings.redis_db,
    )


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def enqueue(func_name: str, *args, **kwargs) -> str | None:
    """Enqueue a job; returns the job id, or None if the queue is unreachable.

    Carries the enqueuing tenant so the worker can re-establish the RLS context."""
    try:
        if "_tenant" not in kwargs:
            tid = get_current_tenant()
            kwargs["_tenant"] = str(tid) if tid else None
        pool = await get_pool()
        job = await pool.enqueue_job(func_name, *args, **kwargs)
        return job.job_id if job else None
    except Exception as exc:  # noqa: BLE001 — never let enqueue break a request
        log.warning("enqueue_failed", func=func_name, error=str(exc))
        return None


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
