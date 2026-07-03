"""The REST chat contract: input shapes and client_msg_id idempotency.

The HTTP fallback must accept the same shapes the WS path does: text-only,
attachment-only (empty text), or both — but never both empty (422). client_msg_id is
optional and bounded so the widget's idempotency key survives the degraded channel too.

Idempotency (WS parity): a resend of an already-persisted client_msg_id must not run a
second turn — /api/chat/message replays the first assistant answer that followed that
user message instead. The replay tests use the dev Postgres and skip when it's not
running (or when RLS blocks the fixture).
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.api.chat.rest as rest_mod
from app.agent.events import AgentEvent
from app.api.chat.rest import MessageIn
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole, SessionStatus

_ATT = [{"url": "/api/chat/media/x.png", "name": "x.png", "kind": "image"}]


def test_text_only_message_is_valid():
    m = MessageIn(text="退货政策是什么？")
    assert m.text and m.attachments == []


def test_attachment_only_message_is_valid():
    m = MessageIn(text="", attachments=_ATT)
    assert m.text == "" and m.attachments == _ATT


def test_missing_text_with_attachments_is_valid():
    m = MessageIn(attachments=_ATT)  # text omitted entirely
    assert m.text == ""


def test_both_empty_rejected():
    with pytest.raises(ValidationError):
        MessageIn(text="", attachments=[])


def test_whitespace_text_without_attachments_rejected():
    with pytest.raises(ValidationError):
        MessageIn(text="   ")


def test_client_msg_id_accepted_and_bounded():
    m = MessageIn(text="hi", client_msg_id="c-123")
    assert m.client_msg_id == "c-123"
    with pytest.raises(ValidationError):
        MessageIn(text="hi", client_msg_id="x" * 65)


# ------------------------------------------------- client_msg_id idempotency (replay)
@pytest.mark.asyncio
async def test_duplicate_reply_guards_skip_lookup():
    """缺 client_msg_id / 缺 session_id / session_id 非法 → 不触库直接放行（db=None 证明）。"""
    assert await rest_mod._duplicate_turn_reply(None, str(uuid.uuid4()), "") is None
    assert await rest_mod._duplicate_turn_reply(None, None, "c-1") is None
    assert await rest_mod._duplicate_turn_reply(None, "not-a-uuid", "c-1") is None


async def _db_up() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import engine

        # Drop pooled connections bound to a previous test's (closed) event loop.
        await engine.dispose(close=False)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_duplicate_client_msg_id_replays_without_rerun(monkeypatch):
    """同会话重复 client_msg_id：直接返回该用户消息之后最近一条 assistant 回答，
    handle_turn 不得被调用；换一个新 client_msg_id 才正常跑轮。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.db.tenant_context import DEFAULT_TENANT_ID, set_current_tenant

    set_current_tenant(DEFAULT_TENANT_ID)
    try:
        # 第一轮已完成后的库内状态：用户消息（携带 client_msg_id 记录）+ assistant 回答。
        async with session_scope() as db:
            session = Session(
                channel_type="web", channel_key="default", end_user_id="rest-dup",
                status=SessionStatus.ACTIVE, last_activity_at=datetime.now(UTC),
                message_count=2,
            )
            db.add(session)
            await db.flush()
            user_msg = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=1,
                                 role=MessageRole.USER, content="退货政策")
            answer = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=2,
                               role=MessageRole.ASSISTANT, content="七天无理由退货",
                               citations=[{"title": "退货规则"}])
            db.add_all([user_msg, answer])
            await db.flush()
            session.meta = {"client_msg_ids": [["c-dup", str(user_msg.id)]]}
            await db.commit()
            sid, answer_id = str(session.id), str(answer.id)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot seed fixture (RLS enforced?): {exc}")

    async def fake_channel(db, key):
        return SimpleNamespace(rate_limit_user_per_min=60, rate_limit_ip_per_min=600)

    async def fake_limits(*args):
        return SimpleNamespace(allowed=True, retry_after=0)

    ran: list[str] = []

    async def fake_turn(db, inbound, client_msg_id=""):
        ran.append(client_msg_id)
        yield AgentEvent(kind="text", text="新回答")
        yield AgentEvent(
            kind="done", text="新回答",
            data={"message_id": "m-new", "session_id": sid, "citations": []},
        )

    monkeypatch.setattr(rest_mod, "resolve_active_web_channel", fake_channel)
    monkeypatch.setattr(rest_mod, "is_origin_allowed", lambda channel, origin: True)
    monkeypatch.setattr(rest_mod, "check_chat_limits", fake_limits)
    monkeypatch.setattr(rest_mod, "handle_turn", fake_turn)

    request = SimpleNamespace(headers={}, client=None)
    try:
        # 重复的 client_msg_id → 返回已有回答（含 citations），绝不重跑。
        async with session_scope() as db:
            res = await rest_mod.chat_message(
                MessageIn(text="退货政策", session_id=sid, client_msg_id="c-dup"),
                request, origin=None, db=db,
            )
        assert res == {
            "reply": "七天无理由退货",
            "message_id": answer_id,
            "session_id": sid,
            "citations": [{"title": "退货规则"}],
            "escalation": None,
        }
        assert ran == []  # handle_turn 未被调用

        # 新 client_msg_id → 不命中幂等短路，正常跑轮。
        async with session_scope() as db:
            res = await rest_mod.chat_message(
                MessageIn(text="发票怎么开", session_id=sid, client_msg_id="c-new"),
                request, origin=None, db=db,
            )
        assert res["reply"] == "新回答"
        assert ran == ["c-new"]
    finally:
        async with session_scope() as db:
            obj = await db.get(Session, uuid.UUID(sid))
            if obj:
                await db.delete(obj)
            await db.commit()


@pytest.mark.asyncio
async def test_duplicate_reply_never_borrows_a_later_turns_answer(monkeypatch):
    """本轮无回答但之后存在「别轮的 assistant」：重复请求绝不把别轮答案当本条回答返回，
    应返回空 reply 且不重跑 handle_turn。一个 turn 是 user(seq N)+assistant(seq N+1);
    若 seq N+1 无 assistant（本轮 stop 无输出），后续 turn 的答案不得被借用。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.db.tenant_context import DEFAULT_TENANT_ID, set_current_tenant

    set_current_tenant(DEFAULT_TENANT_ID)
    try:
        # 库内状态：第一条用户消息(seq 1)本轮无回答；之后又发了一轮并有回答
        # user(seq 2) + assistant(seq 3)。重复的是第一轮的 client_msg_id。
        async with session_scope() as db:
            session = Session(
                channel_type="web", channel_key="default", end_user_id="rest-dup3",
                status=SessionStatus.ACTIVE, last_activity_at=datetime.now(UTC),
                message_count=3,
            )
            db.add(session)
            await db.flush()
            first_user = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=1,
                                   role=MessageRole.USER, content="第一条(无回答)")
            second_user = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=2,
                                    role=MessageRole.USER, content="第二条")
            other_answer = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=3,
                                     role=MessageRole.ASSISTANT, content="别轮的答案",
                                     citations=[{"title": "不该被借用"}])
            db.add_all([first_user, second_user, other_answer])
            await db.flush()
            session.meta = {"client_msg_ids": [["c-first", str(first_user.id)]]}
            await db.commit()
            sid = str(session.id)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot seed fixture (RLS enforced?): {exc}")

    async def fake_channel(db, key):
        return SimpleNamespace(rate_limit_user_per_min=60, rate_limit_ip_per_min=600)

    async def fail_if_run(db, inbound, client_msg_id=""):
        raise AssertionError("duplicate client_msg_id must not rerun the turn")
        yield  # pragma: no cover — make it an async generator

    monkeypatch.setattr(rest_mod, "resolve_active_web_channel", fake_channel)
    monkeypatch.setattr(rest_mod, "is_origin_allowed", lambda channel, origin: True)
    monkeypatch.setattr(rest_mod, "handle_turn", fail_if_run)

    try:
        async with session_scope() as db:
            res = await rest_mod.chat_message(
                MessageIn(text="第一条(无回答)", session_id=sid, client_msg_id="c-first"),
                SimpleNamespace(headers={}, client=None), origin=None, db=db,
            )
        # 别轮的答案(seq 3)不是本轮的 seq+1，绝不被返回。
        assert res["reply"] == "" and res["message_id"] is None
        assert res["citations"] == []
        assert res["session_id"] == sid
    finally:
        async with session_scope() as db:
            obj = await db.get(Session, uuid.UUID(sid))
            if obj:
                await db.delete(obj)
            await db.commit()


@pytest.mark.asyncio
async def test_duplicate_client_msg_id_without_answer_still_skips_rerun(monkeypatch):
    """第一轮尚无 assistant 回答（如中途被 stop 且无部分输出）：重复请求仍不得重跑，
    返回空 reply（契约：重复 client_msg_id 一律不再触发新一轮）。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.db.tenant_context import DEFAULT_TENANT_ID, set_current_tenant

    set_current_tenant(DEFAULT_TENANT_ID)
    try:
        async with session_scope() as db:
            session = Session(
                channel_type="web", channel_key="default", end_user_id="rest-dup2",
                status=SessionStatus.ACTIVE, last_activity_at=datetime.now(UTC),
                message_count=1,
            )
            db.add(session)
            await db.flush()
            user_msg = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=1,
                                 role=MessageRole.USER, content="还没有回答")
            db.add(user_msg)
            await db.flush()
            session.meta = {"client_msg_ids": [["c-pending", str(user_msg.id)]]}
            await db.commit()
            sid = str(session.id)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot seed fixture (RLS enforced?): {exc}")

    async def fake_channel(db, key):
        return SimpleNamespace(rate_limit_user_per_min=60, rate_limit_ip_per_min=600)

    async def fail_if_run(db, inbound, client_msg_id=""):
        raise AssertionError("duplicate client_msg_id must not rerun the turn")
        yield  # pragma: no cover — make it an async generator

    monkeypatch.setattr(rest_mod, "resolve_active_web_channel", fake_channel)
    monkeypatch.setattr(rest_mod, "is_origin_allowed", lambda channel, origin: True)
    monkeypatch.setattr(rest_mod, "handle_turn", fail_if_run)

    try:
        async with session_scope() as db:
            res = await rest_mod.chat_message(
                MessageIn(text="还没有回答", session_id=sid, client_msg_id="c-pending"),
                SimpleNamespace(headers={}, client=None), origin=None, db=db,
            )
        assert res["reply"] == "" and res["message_id"] is None
        assert res["session_id"] == sid
    finally:
        async with session_scope() as db:
            obj = await db.get(Session, uuid.UUID(sid))
            if obj:
                await db.delete(obj)
            await db.commit()
