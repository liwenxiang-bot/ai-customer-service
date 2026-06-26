"""Unit: image turns retry across common OpenAI-compatible gateway limitations."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from app.agent.runner import AgentRunner
from app.agent.tools.base import Tool, ToolContext, ToolRegistry
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import StreamEvent, ToolCall, Usage
from app.models.config import AIConfig
from app.models.conversation import Session
from app.models.enums import SessionStatus


class RecordingProvider(LLMProvider):
    def __init__(self, turns: list[list[StreamEvent]]):
        super().__init__(LLMSettings("fake", "", "", "vision-model"))
        self._turns = turns
        self.calls: list[dict] = []

    async def stream_chat(self, messages, tools=None, **kw) -> AsyncIterator[StreamEvent]:
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": kw.get("tool_choice", "auto")})
        turn = self._turns.pop(0)
        for ev in turn:
            yield ev

    async def complete(self, messages, **kw) -> StreamEvent:
        return StreamEvent(kind="done", text="ok", usage=Usage(1, 1))


class DummyTool(Tool):
    name = "search_knowledge"
    description = "Search knowledge"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict, ctx: ToolContext) -> str:
        return "ok"


class FakeDB:
    def __init__(self):
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(DummyTool())
    return reg


def _session() -> Session:
    return Session(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        channel_type="web",
        channel_key="default",
        end_user_id="u1",
        status=SessionStatus.ACTIVE,
        last_activity_at=datetime.now(UTC),
        summarized_until_seq=0,
        message_count=1,
    )


async def _fake_build_messages(
    db,
    session,
    ai_config,
    channel_system_prompt=None,
    image_understanding=False,
):
    if image_understanding:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "这个是啥"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                ],
            }
        ]
    return [{"role": "user", "content": "这个是啥\n（用户上传了 1 个附件：logo.png）"}]


async def _noop_record_turn_cost(db, session, usage, cost) -> None:
    return None


async def _run(runner: AgentRunner):
    events = []
    async for ev in runner.run_turn(FakeDB(), _session(), "trace-image"):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_image_turn_retries_without_tools(monkeypatch):
    monkeypatch.setattr("app.agent.runner.build_messages", _fake_build_messages)
    monkeypatch.setattr("app.agent.runner.record_turn_cost", _noop_record_turn_cost)
    provider = RecordingProvider(
        [
            [StreamEvent(kind="error", error="images with tools not supported")],
            [
                StreamEvent(kind="text", text="这是一张品牌标志。"),
                StreamEvent(kind="done", usage=Usage(10, 6), finish_reason="stop"),
            ],
        ]
    )
    runner = AgentRunner(provider, _registry(), AIConfig(llm_model="vision-model"), image_understanding=True)

    events = await _run(runner)

    assert [bool(c["tools"]) for c in provider.calls] == [True, False]
    assert "".join(ev.text for ev in events if ev.kind == "text") == "这是一张品牌标志。"
    assert events[-1].data["degraded"] is False


@pytest.mark.asyncio
async def test_image_turn_forces_knowledge_search(monkeypatch):
    """With an image, the first pass must force search_knowledge so the answer stays grounded
    in the KB instead of the model replying to the picture directly."""
    monkeypatch.setattr("app.agent.runner.build_messages", _fake_build_messages)
    monkeypatch.setattr("app.agent.runner.record_turn_cost", _noop_record_turn_cost)
    provider = RecordingProvider(
        [
            # forced first pass → the model calls search_knowledge
            [
                StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="search_knowledge", arguments="{}")]),
                StreamEvent(kind="done", usage=Usage(5, 3), finish_reason="tool_calls"),
            ],
            # second pass → answers from the retrieved context
            [
                StreamEvent(kind="text", text="根据知识库，这是…"),
                StreamEvent(kind="done", usage=Usage(8, 5), finish_reason="stop"),
            ],
        ]
    )
    runner = AgentRunner(provider, _registry(), AIConfig(llm_model="vision-model"), image_understanding=True)

    events = await _run(runner)

    # first call forces the search tool; subsequent calls go back to "auto"
    assert provider.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "search_knowledge"}}
    assert provider.calls[1]["tool_choice"] == "auto"
    assert "".join(ev.text for ev in events if ev.kind == "text") == "根据知识库，这是…"


@pytest.mark.asyncio
async def test_image_turn_falls_back_to_text_context(monkeypatch):
    monkeypatch.setattr("app.agent.runner.build_messages", _fake_build_messages)
    monkeypatch.setattr("app.agent.runner.record_turn_cost", _noop_record_turn_cost)
    provider = RecordingProvider(
        [
            [StreamEvent(kind="error", error="images with tools not supported")],
            [StreamEvent(kind="error", error="image inputs not supported")],
            [
                StreamEvent(kind="text", text="我收到了附件，但当前没有读到图片内容。"),
                StreamEvent(kind="done", usage=Usage(8, 7), finish_reason="stop"),
            ],
        ]
    )
    runner = AgentRunner(provider, _registry(), AIConfig(llm_model="text-model"), image_understanding=True)

    events = await _run(runner)

    assert [bool(c["tools"]) for c in provider.calls] == [True, False, True]
    assert isinstance(provider.calls[-1]["messages"][-1]["content"], str)
    assert "附件" in provider.calls[-1]["messages"][-1]["content"]
    assert "".join(ev.text for ev in events if ev.kind == "text") == "我收到了附件，但当前没有读到图片内容。"
    assert events[-1].data["degraded"] is True
