"""Turn reliability: no DB connection held while the LLM streams; a turn survives the client
disconnecting mid-answer."""

import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.chat.ws as ws_mod
from app.agent.events import AgentEvent
from app.agent.runner import AgentRunner
from app.agent.tools.base import ToolRegistry
from app.config import settings
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import StreamEvent, Usage
from app.main import app
from app.models.conversation import Message, Session
from app.models.enums import MessageRole, SessionStatus
from app.services.ai_config import get_active_ai_config
from tests.test_ws_chat import _patch_connection_deps


class TxProbe(LLMProvider):
    """Records whether a DB transaction was open each time the model was called."""

    def __init__(self, db):
        super().__init__(LLMSettings("fake", "", "", "fake-model"))
        self.db, self.open_tx = db, []

    async def stream_chat(self, messages, tools=None, **kw):
        self.open_tx.append(self.db.in_transaction())
        yield StreamEvent(kind="text", text="好的")
        yield StreamEvent(kind="done", usage=Usage(3, 2), finish_reason="stop")

    async def complete(self, messages, **kw):
        return StreamEvent(kind="done", usage=Usage(1, 1))


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


async def test_no_transaction_is_held_while_the_model_streams(real_db):
    cfg = await get_active_ai_config(real_db)
    session = Session(channel_type="web", channel_key="default", end_user_id=f"u-{uuid.uuid4().hex[:8]}",
                      status=SessionStatus.ACTIVE, last_activity_at=datetime.now(UTC))
    real_db.add(session)
    await real_db.flush()
    real_db.add(Message(tenant_id=session.tenant_id, session_id=session.id, seq=1,
                        role=MessageRole.USER, content="你好"))
    session.message_count = 1
    await real_db.flush()
    assert real_db.in_transaction()  # build_messages etc. will run inside a transaction

    probe = TxProbe(real_db)
    events = [ev async for ev in AgentRunner(probe, ToolRegistry(), cfg).run_turn(real_db, session, "t")]
    assert events[-1].kind == "done"
    assert probe.open_tx == [False]  # the connection went back to the pool before streaming
    await real_db.commit()
    await real_db.delete(session)
    await real_db.commit()


def test_turn_finishes_after_client_disconnects_mid_answer(monkeypatch):
    _patch_connection_deps(monkeypatch)
    finished = {"done": False}

    async def slow_turn(db, inbound, client_msg_id=""):
        yield AgentEvent(kind="text", text="第一句，")
        import asyncio

        await asyncio.sleep(0.3)  # client is gone by now
        yield AgentEvent(kind="text", text="第二句。")
        yield AgentEvent(kind="done", text="第一句，第二句。", data={"message_id": "m1", "session_id": "s1"})
        finished["done"] = True  # = handle_turn's final COMMIT ran

    monkeypatch.setattr(ws_mod, "handle_turn", slow_turn)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/chat?uid=u-gone") as ws:
            assert ws.receive_json()["type"] == "connected"
            ws.send_json({"type": "user_message", "text": "hi"})
            while ws.receive_json()["type"] != "stream_chunk":
                pass
        # socket closed mid-answer
        deadline = time.time() + 3
        while not finished["done"] and time.time() < deadline:
            time.sleep(0.05)
    assert finished["done"], "an in-flight turn must complete (and commit) after a disconnect"
