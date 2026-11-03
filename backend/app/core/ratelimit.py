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
    async with r.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window_sec, nx=True)  # set the window once; never a key without TTL
        count, _ = await pipe.execute()
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

    from app.db.tenant_context import DEFAULT_TENANT_ID, get_current_tenant
    t = get_current_tenant() or DEFAULT_TENANT_ID  # namespace counters per tenant

    if end_user_id:
        ok, retry = await _check(f"rl:{t}:user:{end_user_id}", user_limit)
        if not ok:
            return RateDecision(False, retry, "user")
    if ip:
        ok, retry = await _check(f"rl:{t}:ip:{ip}", ip_limit)
        if not ok:
            return RateDecision(False, retry, "ip")
    return RateDecision(True)


# ---- Admin login brute-force protection ----
# Failures are counted per email (primary: independent of proxies / spoofable IP headers)
# and per IP (secondary, generous). A success clears the email counter. Redis being down
# must not lock every admin out, so these helpers fail open.

LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_FAILS_PER_EMAIL = 5
LOGIN_MAX_FAILS_PER_IP = 30


def _login_keys(email: str, ip: str) -> list[tuple[str, int]]:
    keys = [(f"login:fail:email:{email.strip().lower()}", LOGIN_MAX_FAILS_PER_EMAIL)]
    if ip:
        keys.append((f"login:fail:ip:{ip}", LOGIN_MAX_FAILS_PER_IP))
    return keys


async def login_locked_for(email: str, ip: str) -> int:
    """Seconds until login is allowed again (0 = not locked)."""
    try:
        r = get_redis()
        for key, limit in _login_keys(email, ip):
            count = await r.get(key)
            if count is not None and int(count) >= limit:
                return max(await r.ttl(key), 1)
    except Exception:  # noqa: BLE001 — fail open
        return 0
    return 0


async def record_login_failure(email: str, ip: str) -> None:
    try:
        r = get_redis()
        async with r.pipeline(transaction=True) as pipe:
            for key, _ in _login_keys(email, ip):
                pipe.incr(key)
                pipe.expire(key, LOGIN_WINDOW_SEC, nx=True)
            await pipe.execute()
    except Exception:  # noqa: BLE001
        pass


async def clear_login_failures(email: str) -> None:
    try:
        await get_redis().delete(f"login:fail:email:{email.strip().lower()}")
    except Exception:  # noqa: BLE001
        pass
