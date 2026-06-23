"""Redis-backed multi-dimensional rate limiting (per end-user + per IP).

Fixed-window counters — cheap and good enough to stop a public chat entry from being
hammered into a runaway LLM bill (requirements §5). The cost circuit breaker
(services/usage) is the second, spend-based line of defence.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.core.redis_client import get_redis


@dataclass
class RateDecision:
    allowed: bool
    retry_after: int = 0
    scope: str = ""


async def _check(key: str, limit: int, window_sec: int = 60) -> tuple[bool, int]:
    if limit <= 0:
        return True, 0
    r = get_redis()
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, window_sec)
    if count > limit:
        ttl = await r.ttl(key)
        return False, max(ttl, 1)
    return True, 0


async def check_chat_limits(
    end_user_id: str,
    ip: str,
    user_limit: int | None = None,
    ip_limit: int | None = None,
) -> RateDecision:
    user_limit = settings.rate_limit_user_per_min if user_limit is None else user_limit
    ip_limit = settings.rate_limit_ip_per_min if ip_limit is None else ip_limit

    if end_user_id:
        ok, retry = await _check(f"rl:user:{end_user_id}", user_limit)
        if not ok:
            return RateDecision(False, retry, "user")
    if ip:
        ok, retry = await _check(f"rl:ip:{ip}", ip_limit)
        if not ok:
            return RateDecision(False, retry, "ip")
    return RateDecision(True)
