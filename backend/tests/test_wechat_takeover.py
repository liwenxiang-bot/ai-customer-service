"""WeChat Work: durable callback processing, AI silence during takeover, operator messages
delivered to the customer, and post-commit side effects."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from arq import Retry
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.chat.wechat as wx_api
import app.services.conversation as conv
import app.services.takeover as tk
import app.tasks.wechat_tasks as wx_tasks
from app.agent.events import AgentEvent
from app.channels.base import InboundMessage
from app.config import settings
from app.db.session import _fire_after_commit, drain_after_commit_tasks, run_after_commit
from app.main import app

WX_MSG = {"MsgType": "text", "Content": "人工", "FromUserName": "wx-user-1", "MsgId": "100001"}


# ---- callback: verify → enqueue (deduped by MsgId), never an in-process task ----


def test_callback_enqueues_turn_keyed_by_msgid(monkeypatch):
    calls = []

    async def fake_cfg(db):
        return SimpleNamespace(enabled=True)

    class FakeCrypto:
        def decrypt_message(self, *a):
            return "<xml/>"

    async def spy_enqueue(name, *args, **kw):
        calls.append((name, args, kw))
        return "job"

    monkeypatch.setattr(wx_api, "load_wechat_config", fake_cfg)
    monkeypatch.setattr(wx_api, "build_crypto", lambda cfg: FakeCrypto())
    monkeypatch.setattr(wx_api, "parse_inbound", lambda plain: dict(WX_MSG))
    monkeypatch.setattr(wx_api, "enqueue", spy_enqueue)
    r = TestClient(app).post(
        "/api/wechat/callback?msg_signature=s&timestamp=1&nonce=n",
        content="<xml><Encrypt>abc</Encrypt></xml>",
    )
    assert r.status_code == 200 and r.text == ""
    assert calls == [("wechat_handle_inbound", (WX_MSG,), {"_job_id": "wx-in:100001"})]


# ---- worker: turn → reply queued; takeover → silent ----


def _fake_turn(events):
    async def turn(db, inbound, client_msg_id=""):
        for ev in events:
            yield ev
    return turn


@pytest.fixture
def no_db(monkeypatch):
    class Scope:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(wx_tasks, "session_scope", Scope)


async def test_inbound_job_queues_reply(monkeypatch, no_db):
    sent = []

    async def spy_enqueue(name, *args, **kw):
        sent.append((name, args))

    monkeypatch.setattr(conv, "handle_turn", _fake_turn([
        AgentEvent(kind="text", text="您好，"), AgentEvent(kind="text", text="请问有什么可以帮您？"),
        AgentEvent(kind="done", text="", data={"message_id": "m1"}),
    ]))
    monkeypatch.setattr("app.tasks.queue.enqueue", spy_enqueue)
    assert await wx_tasks.wechat_handle_inbound({}, dict(WX_MSG)) == "queued"
    assert sent == [("wechat_send", ("wx-user-1", "您好，请问有什么可以帮您？"))]


async def test_inbound_job_stays_silent_during_takeover(monkeypatch, no_db):
    sent = []

    async def spy_enqueue(name, *args, **kw):
        sent.append(name)

    monkeypatch.setattr(conv, "handle_turn", _fake_turn([
        AgentEvent(kind="done", text="", data={"takeover": True}),
    ]))
    monkeypatch.setattr("app.tasks.queue.enqueue", spy_enqueue)
    assert await wx_tasks.wechat_handle_inbound({}, dict(WX_MSG)) == "takeover"
    assert sent == []


async def test_send_job_retries_then_gives_up(monkeypatch, no_db):
    async def cfg(db):
        return SimpleNamespace(enabled=True, secret="s", corp_id="c", agent_id="1")

    async def failing_send(*a):
        return False

    monkeypatch.setattr(wx_tasks, "load_wechat_config", cfg)
    monkeypatch.setattr("app.services.wechat_client.send_text", failing_send)
    with pytest.raises(Retry):
        await wx_tasks.wechat_send({"job_try": 1}, "wx-user-1", "hi")
    assert await wx_tasks.wechat_send({"job_try": wx_tasks.SEND_MAX_TRIES}, "wx-user-1", "hi") is False


# ---- operator side reaches WeChat customers after commit ----


class _FakeDB:
    def __init__(self):
        self.sync_session = SimpleNamespace(info={})


async def test_operator_messages_delivered_to_wechat_after_commit(monkeypatch):
    sent = []

    async def spy_enqueue(name, *args, **kw):
        sent.append((name, args))

    monkeypatch.setattr("app.tasks.queue.enqueue", spy_enqueue)
    db = _FakeDB()
    wechat = SimpleNamespace(channel_type="wechat_work", end_user_id="wx-user-1", tenant_id=uuid.uuid4())
    web = SimpleNamespace(channel_type="web", end_user_id="u-1", tenant_id=uuid.uuid4())
    tk.deliver_offsite(db, wechat, "人工客服已接入")
    tk.deliver_offsite(db, web, "不应发送")
    assert sent == []  # nothing before commit
    _fire_after_commit(db.sync_session)
    await drain_after_commit_tasks()
    assert sent == [("wechat_send", ("wx-user-1", "人工客服已接入"))]


# ---- post-commit hook against a real database ----


@pytest.fixture
async def real_db():
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


async def test_after_commit_runs_on_commit_and_is_dropped_on_rollback(real_db):
    ran = []

    async def mark(tag):
        ran.append(tag)

    from sqlalchemy import text

    await real_db.execute(text("SELECT 1"))
    run_after_commit(real_db, lambda: mark("rolled-back"))
    await real_db.rollback()
    await real_db.execute(text("SELECT 1"))
    run_after_commit(real_db, lambda: mark("committed"))
    await real_db.commit()
    await drain_after_commit_tasks()
    await asyncio.sleep(0)
    assert ran == ["committed"]


# ---- the shared turn handler keeps the AI silent during takeover (REST + WeChat) ----


async def test_handle_turn_is_silent_during_takeover(monkeypatch, real_db):
    pushed = []

    async def taken_over(sid):
        return True

    async def spy_publish(sid, payload):
        pushed.append(payload)

    class NoAI:
        def __init__(self, *a, **k):
            raise AssertionError("AI must not run during takeover")

    monkeypatch.setattr(conv, "is_takeover", taken_over)
    monkeypatch.setattr(conv, "publish", spy_publish)
    monkeypatch.setattr(conv, "AgentRunner", NoAI)
    inbound = InboundMessage(channel_type="wechat_work", channel_key="default",
                             end_user_id=f"wx-{uuid.uuid4().hex[:8]}", text="在吗")
    events = [ev async for ev in conv.handle_turn(real_db, inbound)]
    assert [e.kind for e in events] == ["done"] and events[0].data["takeover"] is True
    assert pushed and pushed[0]["type"] == "customer_message" and pushed[0]["content"] == "在吗"
    from app.models.conversation import Session

    s = await real_db.get(Session, uuid.UUID(inbound.session_id))
    await real_db.delete(s)
    await real_db.commit()


# ---- workbench list: last message preview (one query per page) ----


async def test_conversation_list_has_last_message_preview(real_db):
    from datetime import UTC, datetime

    from app.api.admin.conversations import _last_message_previews
    from app.models.conversation import Message, Session
    from app.models.enums import MessageRole, SessionStatus

    s = Session(channel_type="web", channel_key="default", end_user_id=f"u-{uuid.uuid4().hex[:8]}",
                status=SessionStatus.ESCALATED, last_activity_at=datetime.now(UTC))
    real_db.add(s)
    await real_db.flush()
    real_db.add_all([
        Message(tenant_id=s.tenant_id, session_id=s.id, seq=1, role=MessageRole.USER, content="退货流程？"),
        Message(tenant_id=s.tenant_id, session_id=s.id, seq=2, role=MessageRole.ASSISTANT, model="human",
                content="您好，\n请提供订单号"),
        Message(tenant_id=s.tenant_id, session_id=s.id, seq=3, role=MessageRole.USER, content="",
                attachments=[{"kind": "image", "url": "/x.png"}]),
    ])
    await real_db.flush()
    previews = await _last_message_previews(real_db, [s.id])
    assert previews[s.id] == {"from": "customer", "text": "[图片]"}
    await real_db.rollback()
