"""Integration: the chat WebSocket endpoint — reader/worker split, stop, ACK, history.

Covers the WS contract: ping is answered while a turn is streaming, every enqueued
user_message is ACKed with {"type":"received", client_msg_id}, {"type":"stop"} cancels
the in-flight generation and persists the partial answer (message_end {stopped: true})
— but a stop landing after the normal message_end is ignored (no duplicate persist, no
second end), a resend of an already-persisted client_msg_id never reruns the turn, the
bounded turn queue refuses overflow with a rate_limited frame, policy rejections close
with 4403, unknown frame types are silently ignored, and `history` returns the latest N
messages ascending with client_msg_id echoed on user messages. Connection deps
(tenant/channel/rate limit) and the LLM turn are faked at the module boundary so no
external service is needed; the history test uses the dev Postgres and skips when it's
not running (or when RLS blocks the fixture).
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

import app.api.chat.ws as ws_mod
from app.agent.events import AgentEvent
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole, SessionStatus

STOP_SID = "11111111-1111-1111-1111-111111111111"


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(ws_mod.router)
    return TestClient(app)


def _patch_connection_deps(monkeypatch) -> None:
    """Fake tenant/channel resolution, rate limits and the push listener (no DB/Redis)."""
    tid = uuid.uuid4()

    async def tenant_for_channel(db, channel_type, key):
        return tid

    async def get_web_channel(db, key):
        return SimpleNamespace(enabled=True, rate_limit_user_per_min=60, rate_limit_ip_per_min=600)

    async def check_chat_limits(*args):
        return SimpleNamespace(allowed=True, retry_after=0)

    async def is_takeover(sid):
        return False

    async def push_listener(send, sid):  # keep the tests off the real Redis
        await asyncio.sleep(3600)

    monkeypatch.setattr(ws_mod, "tenant_for_channel", tenant_for_channel)
    monkeypatch.setattr(ws_mod, "get_web_channel", get_web_channel)
    monkeypatch.setattr(ws_mod, "is_origin_allowed", lambda channel, origin: True)
    monkeypatch.setattr(ws_mod, "check_chat_limits", check_chat_limits)
    monkeypatch.setattr(ws_mod, "is_takeover", is_takeover)
    monkeypatch.setattr(ws_mod, "_push_listener", push_listener)


# ------------------------------------------------------- ping / stop while generating
def test_ping_and_stop_while_generating(monkeypatch):
    _patch_connection_deps(monkeypatch)

    async def slow_turn(db, inbound, client_msg_id=""):
        yield AgentEvent(kind="text", text="部分")
        await asyncio.sleep(30)  # the stop frame cancels us long before this returns
        yield AgentEvent(kind="done", text="部分", data={"message_id": "m1", "session_id": STOP_SID})

    persisted: list[tuple[str, str, str]] = []

    async def fake_persist(inbound, partial, client_msg_id=""):
        persisted.append((inbound.text, partial, client_msg_id))
        return (STOP_SID, "m-stop")

    monkeypatch.setattr(ws_mod, "handle_turn", slow_turn)
    monkeypatch.setattr(ws_mod, "_persist_stopped_turn", fake_persist)

    with _make_client().websocket_connect("/ws/chat?uid=u1") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "text": "退货政策", "client_msg_id": "c-stop"})
        ack = ws.receive_json()
        assert ack["type"] == "received" and ack["client_msg_id"] == "c-stop"
        assert ws.receive_json()["type"] == "message_start"
        chunk = ws.receive_json()
        assert chunk["type"] == "stream_chunk" and chunk["delta"] == "部分"

        # The reader keeps answering ping while the turn streams (no more starvation
        # that let client heartbeat watchdogs kill the connection mid-answer).
        t0 = time.monotonic()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        assert time.monotonic() - t0 < 5

        # stop → generation cancelled, partial persisted, stopped message_end emitted.
        ws.send_json({"type": "stop"})
        end = ws.receive_json()
        assert end["type"] == "message_end"
        assert end["stopped"] is True
        assert end["message_id"] == "m-stop"
        assert end["session_id"] == STOP_SID
        assert persisted == [("退货政策", "部分", "c-stop")]

        # The connection stays usable after a stop.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_stop_after_message_end_does_not_reclose_turn(monkeypatch):
    """A stop racing the post-done window (cache store + COMMIT still running after
    message_end went out) must be ignored: no re-persist, no second message_end."""
    _patch_connection_deps(monkeypatch)
    sid = str(uuid.uuid4())

    async def turn_with_slow_commit(db, inbound, client_msg_id=""):
        yield AgentEvent(kind="text", text="完")
        yield AgentEvent(
            kind="done", text="完", data={"message_id": "m1", "session_id": sid, "citations": []}
        )
        await asyncio.sleep(30)  # the post-done commit window the stop frame lands in

    stopped_persists: list[str] = []

    async def spy_persist(inbound, partial, client_msg_id=""):
        stopped_persists.append(partial)
        return (sid, "m-dup")

    monkeypatch.setattr(ws_mod, "handle_turn", turn_with_slow_commit)
    monkeypatch.setattr(ws_mod, "_persist_stopped_turn", spy_persist)

    with _make_client().websocket_connect("/ws/chat?uid=u5") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "text": "hi"})
        assert ws.receive_json()["type"] == "received"
        assert ws.receive_json()["type"] == "message_start"
        assert ws.receive_json()["type"] == "stream_chunk"
        end = ws.receive_json()
        assert end["type"] == "message_end" and "stopped" not in end

        # The turn is already closed on the wire — this stop must be a no-op.
        ws.send_json({"type": "stop"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # nothing (esp. no 2nd end) before it
        assert stopped_persists == []


def test_turn_streams_to_normal_message_end(monkeypatch):
    _patch_connection_deps(monkeypatch)
    sid = str(uuid.uuid4())

    async def fast_turn(db, inbound, client_msg_id=""):
        yield AgentEvent(kind="text", text="你好")
        yield AgentEvent(
            kind="done", text="你好", data={"message_id": "m9", "session_id": sid, "citations": []}
        )

    monkeypatch.setattr(ws_mod, "handle_turn", fast_turn)

    with _make_client().websocket_connect("/ws/chat?uid=u2") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "text": "hi"})
        assert ws.receive_json()["type"] == "received"
        assert ws.receive_json()["type"] == "message_start"
        chunk = ws.receive_json()
        assert chunk["type"] == "stream_chunk" and chunk["delta"] == "你好"
        end = ws.receive_json()
        assert end["type"] == "message_end"
        assert end["session_id"] == sid
        assert "stopped" not in end  # normal completion is unchanged on the wire


def test_unknown_types_and_idle_stop_ignored(monkeypatch):
    _patch_connection_deps(monkeypatch)

    with _make_client().websocket_connect("/ws/chat?uid=u3") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "made_up_event", "foo": 1})  # unknown → silently ignored
        ws.send_json({"type": "stop"})  # nothing generating → no-op
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # nothing surfaced before the pong


# --------------------------------------------------- received ACK / idempotent resend
def test_duplicate_client_msg_id_skips_rerun(monkeypatch):
    """A resend of an already-persisted client_msg_id is ACKed again but never runs a
    second turn (the widget retries after a lost ACK; the answer must not duplicate)."""
    _patch_connection_deps(monkeypatch)
    sid = str(uuid.uuid4())
    ran: list[str] = []

    async def turn(db, inbound, client_msg_id=""):
        ran.append(client_msg_id)
        yield AgentEvent(
            kind="done", text="好的", data={"message_id": "m1", "session_id": sid, "citations": []}
        )

    seen: set[str] = set()

    async def fake_seen(session_id, client_msg_id):
        return client_msg_id in seen

    monkeypatch.setattr(ws_mod, "handle_turn", turn)
    monkeypatch.setattr(ws_mod, "_client_msg_seen", fake_seen)

    with _make_client().websocket_connect(f"/ws/chat?uid=u6&session_id={sid}") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "text": "问题", "client_msg_id": "c-1"})
        ack = ws.receive_json()
        assert ack["type"] == "received" and ack["client_msg_id"] == "c-1"
        assert ws.receive_json()["type"] == "message_start"
        assert ws.receive_json()["type"] == "message_end"

        seen.add("c-1")  # the first turn persisted it
        ws.send_json({"type": "user_message", "text": "问题", "client_msg_id": "c-1"})
        ack = ws.receive_json()
        assert ack["type"] == "received" and ack["client_msg_id"] == "c-1"  # re-ACKed
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"  # no message_start for the dup
        assert ran == ["c-1"]


# ------------------------------------------------------------- bounded turn queue
def test_turn_queue_overflow_rejects_with_rate_limited(monkeypatch):
    """Once the per-connection queue is full the reader refuses new turns immediately
    (rate_limited frame w/ client_msg_id) instead of buffering without bound."""
    _patch_connection_deps(monkeypatch)

    async def stuck_turn(db, inbound, client_msg_id=""):
        yield AgentEvent(kind="text", text="…")
        await asyncio.sleep(60)  # keep the worker busy so queued turns pile up
        yield AgentEvent(kind="done", text="…", data={"message_id": "m1", "session_id": None})

    monkeypatch.setattr(ws_mod, "handle_turn", stuck_turn)

    with _make_client().websocket_connect("/ws/chat?uid=u7") as ws:
        assert ws.receive_json()["type"] == "connected"
        ws.send_json({"type": "user_message", "text": "m0"})
        assert ws.receive_json()["type"] == "received"
        assert ws.receive_json()["type"] == "message_start"  # worker dequeued m0
        assert ws.receive_json()["type"] == "stream_chunk"

        for i in range(ws_mod.TURN_QUEUE_MAX):  # fill the queue behind the stuck turn
            ws.send_json({"type": "user_message", "text": f"q{i}", "client_msg_id": f"c{i}"})
            ack = ws.receive_json()
            assert ack["type"] == "received" and ack["client_msg_id"] == f"c{i}"

        ws.send_json({"type": "user_message", "text": "溢出", "client_msg_id": "c-over"})
        frame = ws.receive_json()
        assert frame["type"] == "rate_limited"
        assert frame["client_msg_id"] == "c-over"
        assert frame["retry_after"] > 0

        ws.send_json({"type": "ping"})  # the connection itself stays healthy
        assert ws.receive_json()["type"] == "pong"


# ------------------------------------------------------------- policy rejection (4403)
def test_unknown_channel_rejected_with_4403(monkeypatch):
    async def no_tenant(db, channel_type, key):
        return None

    monkeypatch.setattr(ws_mod, "tenant_for_channel", no_tenant)

    with pytest.raises(WebSocketDisconnect) as exc_info:  # noqa: SIM117 — close on receive
        with _make_client().websocket_connect("/ws/chat?uid=u8") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4403


def test_disallowed_origin_rejected_with_4403(monkeypatch):
    _patch_connection_deps(monkeypatch)
    monkeypatch.setattr(ws_mod, "is_origin_allowed", lambda channel, origin: False)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _make_client().websocket_connect("/ws/chat?uid=u9") as ws:
            ws.receive_json()
    assert exc_info.value.code == 4403


# --------------------------------------------------------- client_msg_id bookkeeping
def test_record_client_msg_id_dedupes_and_bounds():
    from app.services.conversation import _record_client_msg_id

    session = SimpleNamespace(meta={})
    _record_client_msg_id(session, "a", "m1")
    _record_client_msg_id(session, "b", "m2")
    _record_client_msg_id(session, "a", "m3")  # re-persist replaces, newest last
    assert session.meta["client_msg_ids"] == [["b", "m2"], ["a", "m3"]]

    for i in range(250):
        _record_client_msg_id(session, f"c{i}", f"m{i}")
    assert len(session.meta["client_msg_ids"]) == 200  # bounded


# ----------------------------------------------------------------------- history
async def _db_up() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_send_history_returns_latest_window_ascending():
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.db.tenant_context import DEFAULT_TENANT_ID, set_current_tenant

    # RLS is fail-closed: without a tenant context the app role can't insert/select.
    set_current_tenant(DEFAULT_TENANT_ID)

    try:
        async with session_scope() as db:
            session = Session(
                channel_type="web", channel_key="default", end_user_id="hist-test",
                status=SessionStatus.ACTIVE, last_activity_at=datetime.now(UTC),
            )
            db.add(session)
            await db.flush()
            first_user_msg: DBMessage | None = None
            for i in range(1, 7):
                role = MessageRole.USER if i % 2 else MessageRole.ASSISTANT
                msg = DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=i,
                                role=role, content=f"m{i}")
                db.add(msg)
                if first_user_msg is None:
                    first_user_msg = msg
            # Tool messages are internal and must never appear in the transcript.
            db.add(DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=7,
                             role=MessageRole.TOOL, content="t7"))
            session.message_count = 7
            await db.flush()
            # The widget's idempotency key mapping — echoed on history user messages.
            session.meta = {"client_msg_ids": [["c-hist-1", str(first_user_msg.id)]]}
            await db.commit()
            sid = str(session.id)
    except Exception as exc:  # noqa: BLE001 — e.g. RLS-enforced env without this tenant
        pytest.skip(f"cannot seed history fixture (RLS enforced?): {exc}")

    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    try:
        # Window smaller than the session → the latest 3, still oldest→newest.
        await ws_mod._send_history(send, sid, 3)
        assert [m["content"] for m in sent[0]["messages"]] == ["m4", "m5", "m6"]
        # Window larger than the session → everything, ascending.
        await ws_mod._send_history(send, sid, 50)
        assert [m["content"] for m in sent[1]["messages"]] == [f"m{i}" for i in range(1, 7)]
        # client_msg_id round-trips on the user message it belongs to — and only there.
        by_content = {m["content"]: m for m in sent[1]["messages"]}
        assert by_content["m1"]["client_msg_id"] == "c-hist-1"
        assert by_content["m2"]["client_msg_id"] is None
    finally:
        async with session_scope() as db:
            obj = await db.get(Session, uuid.UUID(sid))
            if obj:
                await db.delete(obj)
            await db.commit()
