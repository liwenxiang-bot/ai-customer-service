"""WeChat carries no session id: consecutive messages from one user must continue the same
conversation (previously every message opened a brand-new session and lost all context)."""

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.channels.base import InboundMessage
from app.config import settings
from app.models.conversation import Session
from app.services.conversation import get_or_create_session


@pytest.fixture
async def db():
    engine = create_async_engine(settings.app_sqlalchemy_url)
    try:
        async with engine.connect() as conn:
            await conn.close()
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("postgres not available")
    created: list[uuid.UUID] = []
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.info["created"] = created
        yield s
        await s.rollback()
        if created:
            await s.execute(delete(Session).where(Session.id.in_(created)))
            await s.commit()
    await engine.dispose()


def _wx(openid: str) -> InboundMessage:
    return InboundMessage(channel_type="wechat", channel_key="default", end_user_id=openid, text="你好")


async def _get(db, inbound):
    s, created = await get_or_create_session(db, inbound)
    await db.commit()
    if created:
        db.info["created"].append(s.id)
    return s, created


async def test_wechat_messages_continue_the_same_session(db):
    openid = f"wx-{uuid.uuid4().hex[:8]}"
    first, created1 = await _get(db, _wx(openid))
    second, created2 = await _get(db, _wx(openid))
    assert created1 and not created2
    assert second.id == first.id


async def test_wechat_users_do_not_share_sessions(db):
    a, _ = await _get(db, _wx(f"wx-{uuid.uuid4().hex[:8]}"))
    b, _ = await _get(db, _wx(f"wx-{uuid.uuid4().hex[:8]}"))
    assert a.id != b.id


async def test_web_without_session_id_still_starts_fresh(db):
    uid = f"u-{uuid.uuid4().hex[:8]}"
    web = InboundMessage(channel_type="web", channel_key="default", end_user_id=uid, text="hi")
    a, _ = await _get(db, web)
    b, _ = await _get(db, web)
    assert a.id != b.id  # web resumes only via its own session id
