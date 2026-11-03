"""Taking over claims the open ticket; a colleague's session can't be silently grabbed."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.admin.conversations as conv_api
import app.services.takeover as tk
from app.config import settings
from app.models.conversation import HandoffTicket, Session
from app.models.enums import AdminRole, HandoffStatus, SessionStatus


class _FakeRedis:
    async def set(self, *a, **k):
        return True

    async def delete(self, *a, **k):
        return 1

    async def publish(self, *a, **k):
        return 0


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setattr(tk, "get_redis", lambda: _FakeRedis())
    engine = create_async_engine(settings.app_sqlalchemy_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("postgres not available")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


def _op(role=AdminRole.OPERATOR, name="坐席A"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, name=name, email=f"{uuid.uuid4().hex[:6]}@x.com",
                           tenant_id=None)


async def _escalated_session(db):
    s = Session(channel_type="web", channel_key="default", end_user_id=f"u-{uuid.uuid4().hex[:8]}",
                status=SessionStatus.ESCALATED, escalated=True, last_activity_at=datetime.now(UTC))
    db.add(s)
    await db.flush()
    t = HandoffTicket(tenant_id=s.tenant_id, session_id=s.id, channel_type="web", end_user_id=s.end_user_id,
                      status=HandoffStatus.OPEN)
    db.add(t)
    await db.commit()
    return s, t


async def test_takeover_claims_open_ticket_and_blocks_colleagues(db):
    s, t = await _escalated_session(db)
    sid = s.id
    a, b, boss = _op(name="坐席A"), _op(name="坐席B"), _op(AdminRole.ADMIN, "主管")
    try:
        await conv_api.takeover(str(s.id), db=db, user=a)
        await db.refresh(t)
        assert t.status == HandoffStatus.IN_PROGRESS and t.assignee_id == a.id

        with pytest.raises(HTTPException) as e:
            await conv_api.takeover(str(s.id), db=db, user=b)
        assert e.value.status_code == 409 and "坐席A" in e.value.detail

        await conv_api.takeover(str(s.id), db=db, user=boss)  # supervisor override
        await db.refresh(s)
        assert s.takeover_by_id == boss.id
    finally:
        await db.rollback()
        await db.delete(await db.get(Session, sid))
        await db.commit()


async def test_closed_session_cannot_be_taken_over(db):
    s, _ = await _escalated_session(db)
    sid = s.id
    s.status = SessionStatus.CLOSED
    await db.commit()
    try:
        with pytest.raises(HTTPException) as e:
            await conv_api.takeover(str(sid), db=db, user=_op())
        assert e.value.status_code == 409
    finally:
        await db.rollback()
        await db.delete(await db.get(Session, sid))
        await db.commit()
