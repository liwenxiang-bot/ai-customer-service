"""The tool loop must not present a failed or truncated LLM answer as a normal one.

Regression for the audit finding: after a tool call, a later provider error was swallowed —
the canned apology (or a half-streamed answer) was persisted with degraded=False, counted as
"ok" in metrics and became eligible for the semantic cache.
"""

from types import SimpleNamespace

import pytest

import app.agent.runner as runner_mod
from app.agent.runner import INTERRUPTED_NOTE, TRUNCATED_NOTE, AgentRunner
from app.agent.tools.base import Tool, ToolRegistry
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import StreamEvent, ToolCall, Usage


class Scripted(LLMProvider):
    def __init__(self, turns):
        super().__init__(LLMSettings("fake", "", "", "fake-model"))
        self.turns, self.calls = turns, 0

    async def stream_chat(self, messages, tools=None, **kw):
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        for ev in turn:
            yield ev

    async def complete(self, messages, **kw):
        return StreamEvent(kind="done", usage=Usage(1, 1))


class EchoTool(Tool):
    name = "lookup"
    description = "test tool"

    def __init__(self):
        self.runs = 0

    async def run(self, args, ctx):
        self.runs += 1
        return "资料：支持 7 天无理由退货"


@pytest.fixture
def harness(monkeypatch):
    persisted = {}

    async def _build(*a, **k):
        return [{"role": "user", "content": "能退货吗"}]

    async def _persist(self, db, session, text, trace, citations, trace_id, usage, cost, lat, degraded,
                       answer_kind=""):
        persisted.update(text=text, degraded=degraded, tools=len(trace), answer_kind=answer_kind)
        return SimpleNamespace(id="m1")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(runner_mod, "build_messages", _build)
    monkeypatch.setattr(runner_mod, "needs_summarization", lambda s: False)
    monkeypatch.setattr(runner_mod, "record_turn_cost", _noop)
    monkeypatch.setattr(AgentRunner, "_persist_assistant", _persist)
    tool = EchoTool()
    reg = ToolRegistry()
    reg.register(tool)
    cfg = SimpleNamespace(llm_model="fake-model")

    async def run(turns):
        r = AgentRunner(Scripted(turns), reg, cfg)
        events = [ev async for ev in r.run_turn(None, SimpleNamespace(id="s1"), "t")]
        return events[-1], persisted, tool

    return run


TOOL_CALL = [
    StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="lookup", arguments="{}")]),
    StreamEvent(kind="done", usage=Usage(5, 0), finish_reason="tool_calls"),
]
ERROR = [StreamEvent(kind="error", error="HTTP 503")]


async def test_error_after_tool_call_is_degraded(harness):
    done, saved, _ = await harness([TOOL_CALL, ERROR, ERROR])
    assert saved["degraded"] is True and done.data["degraded"] is True
    assert saved["tools"] == 1
    assert "抱歉" in saved["text"]


async def test_stream_broken_mid_answer_is_degraded_and_marked(harness):
    partial = [StreamEvent(kind="text", text="根据资料，支持"), StreamEvent(kind="error", error="read timeout")]
    done, saved, _ = await harness([TOOL_CALL, partial])
    assert saved["degraded"] is True
    assert saved["text"] == "根据资料，支持" + INTERRUPTED_NOTE


async def test_tool_calls_from_a_broken_stream_are_not_executed(harness):
    broken = [
        StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="c2", name="lookup", arguments="{}")]),
        StreamEvent(kind="error", error="connection reset"),
    ]
    _, saved, tool = await harness([TOOL_CALL, broken])
    assert tool.runs == 1  # only the first, healthy call ran
    assert saved["degraded"] is True


async def test_truncated_answer_is_degraded_and_marked(harness):
    long = [StreamEvent(kind="text", text="第一点……"), StreamEvent(kind="done", usage=Usage(5, 1024), finish_reason="length")]
    done, saved, _ = await harness([long])
    assert saved["degraded"] is True
    assert saved["text"] == "第一点……" + TRUNCATED_NOTE


async def test_healthy_answer_is_not_degraded(harness):
    ok = [StreamEvent(kind="text", text="支持 7 天无理由退货。"), StreamEvent(kind="done", usage=Usage(5, 9), finish_reason="stop")]
    done, saved, _ = await harness([TOOL_CALL, ok])
    assert saved["degraded"] is False
    assert saved["text"] == "支持 7 天无理由退货。"
