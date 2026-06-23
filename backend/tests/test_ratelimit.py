"""Unit/infra: rate limiting (requires the dev Redis to be running).

Skips automatically if Redis is unreachable so the suite stays green offline.
"""

import uuid

import pytest

from app.core.ratelimit import check_chat_limits
from app.core.redis_client import close_redis, get_redis


async def _redis_up() -> bool:
    # Reset the pool so it binds to the current test's event loop.
    await close_redis()
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


@pytest.mark.asyncio
async def test_user_rate_limit_trips():
    if not await _redis_up():
        pytest.skip("redis not available")
    uid = f"test-{uuid.uuid4().hex}"
    allowed = 0
    for _ in range(10):
        d = await check_chat_limits(uid, "", user_limit=3, ip_limit=0)
        if d.allowed:
            allowed += 1
    assert allowed == 3  # exactly the limit passes, the rest are blocked
    await close_redis()


@pytest.mark.asyncio
async def test_zero_limit_disables():
    if not await _redis_up():
        pytest.skip("redis not available")
    d = await check_chat_limits(f"t-{uuid.uuid4().hex}", "", user_limit=0, ip_limit=0)
    assert d.allowed is True
    await close_redis()
