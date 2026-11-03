"""A session id alone must not grant access to someone else's conversation."""

import uuid
from types import SimpleNamespace

import app.api.chat.ws as ws_mod
from app.services.conversation import owned_session
from app.services.feedback import set_feedback
from tests.test_ws_chat import _make_client, _patch_connection_deps


class _DB:
    def __init__(self, obj):
        self.obj = obj

    async def get(self, model, key):
        return self.obj if self.obj is not None and self.obj.id == key else None

    async def flush(self):
        pass


SID = uuid.uuid4()
SESSION = SimpleNamespace(id=SID, end_user_id="u-alice", channel_type="web")


async def test_owned_session_requires_matching_uid_and_channel():
    db = _DB(SESSION)
    assert await owned_session(db, str(SID), "u-alice") is SESSION
    assert await owned_session(db, str(SID), "u-alice", "web") is SESSION
    assert await owned_session(db, str(SID), "u-mallory") is None
    assert await owned_session(db, str(SID), "") is None
    assert await owned_session(db, str(SID), "u-alice", "wechat") is None
    assert await owned_session(db, "not-a-uuid", "u-alice") is None


async def test_feedback_cannot_target_another_sessions_message():
    msg = SimpleNamespace(id=uuid.uuid4(), session_id=SID, tool_calls=[], citations=[], feedback=None, feedback_note="")
    db = _DB(msg)
    assert await set_feedback(db, str(msg.id), "up", session_id=str(uuid.uuid4())) is False
    assert msg.feedback is None
    assert await set_feedback(db, str(msg.id), "up", note="x" * 5000, session_id=str(SID)) is True
    assert msg.feedback == "up" and len(msg.feedback_note) == 1000


def test_ws_ignores_session_id_not_owned_by_uid(monkeypatch):
    _patch_connection_deps(monkeypatch)
    subscribed = []

    async def not_owned(sid, uid):
        return False

    async def spy_listener(send, sid):
        subscribed.append(sid)

    monkeypatch.setattr(ws_mod, "_session_owned_by", not_owned)
    monkeypatch.setattr(ws_mod, "_push_listener", spy_listener)
    with _make_client().websocket_connect(f"/ws/chat?uid=u-mallory&session_id={SID}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "connected" and hello["session_id"] is None
        ws.send_json({"type": "feedback", "message_id": str(uuid.uuid4()), "kind": "up"})
        assert ws.receive_json() == {"type": "feedback_ack", "ok": False}
    assert subscribed == []  # never subscribed to the victim's operator pushes
