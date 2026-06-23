"""Integration: the agent tool-loop end-to-end with a fake LLM provider + real DB.

Covers §15: full conversation chain, tool loop, and graceful persistence. Requires the
dev Postgres to be running; skips otherwise.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from app.agent.runner import AgentRunner
from app.agent.tools import build_default_registry
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import StreamEvent, ToolCall, Usage
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole, SessionStatus
from app.services.ai_config import get_active_ai_config


class ScriptedProvider(LLMProvider):
    """Yields pre-scripted turns; first turn requests a tool, second answers."""

    def __init__(self, turns: list[list[StreamEvent]]):
        super().__init__(LLMSettings("fake", "", "", "fake-model"))
        self._turns = turns
        self._i = 0

    async def stream_chat(self, messages, tools=None, **kw) -> AsyncIterator[StreamEvent]:
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1
        for ev in turn:
            yield ev

    async def complete(self, messages, **kw) -> StreamEvent:
        return StreamEvent(kind="done", text="ok", usage=Usage(1, 1))


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
async def test_tool_loop_runs_and_persists():
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope

    turns = [
        # Turn 1: model asks to search the knowledge base.
        [
            StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="search_knowledge", arguments='{"query":"退货"}')]),
            StreamEvent(kind="done", usage=Usage(20, 0), finish_reason="tool_calls"),
        ],
        # Turn 2: model produces the final answer.
        [
            StreamEvent(kind="text", text="根据资料，"),
            StreamEvent(kind="text", text="支持7天无理由退货。"),
            StreamEvent(kind="done", usage=Usage(30, 12), finish_reason="stop"),
        ],
    ]

    async with session_scope() as db:
        ai_config = await get_active_ai_config(db)
        session = Session(
            channel_type="web", channel_key="default", end_user_id="itest",
            status=SessionStatus.ACTIVE, last_activity_at=datetime.now(timezone.utc),
        )
        db.add(session)
        await db.flush()
        # Seed the user message so context has something.
        db.add(DBMessage(tenant_id=session.tenant_id, session_id=session.id, seq=1,
                         role=MessageRole.USER, content="你们支持退货吗"))
        session.message_count = 1
        await db.flush()

        runner = AgentRunner(ScriptedProvider(turns), build_default_registry(), ai_config)
        kinds, final_text = [], ""
        async for ev in runner.run_turn(db, session, "trace-itest"):
            kinds.append(ev.kind)
            if ev.kind == "done":
                final_text = ev.text

        # The tool was invoked (status events present) and a final answer streamed.
        assert "tool_status" in kinds
        assert "支持7天无理由退货" in final_text
        assert kinds[-1] == "done"

        # Assistant message persisted with the tool-call trace.
        saved = (await db.execute(
            DBMessage.__table__.select().where(
                (DBMessage.session_id == session.id) & (DBMessage.role == MessageRole.ASSISTANT)
            )
        )).first()
        assert saved is not None

        # Cleanup.
        await db.delete(session)
        await db.commit()
