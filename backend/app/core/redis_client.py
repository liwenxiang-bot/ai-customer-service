"""Shared async Redis client (connection pool).

Redis backs: session context cache, rate-limit counters, the daily cost circuit
breaker, and WebSocket presence. One pool per process.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _pool
    if _pool is None:
        _pool = aioredis.from_url(
            settings.redis_dsn,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.aclose()
        except Exception:
            # Tolerate cross-event-loop close (e.g. pytest function-scoped loops).
            pass
        _pool = None
