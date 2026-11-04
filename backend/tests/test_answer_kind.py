"""Unit + runner-level: answer classification, citation cleaning, forced first-pass
search, and the knowledge-gap grouping."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.agent.runner as runner_mod
from app.agent.runner import AgentRunner
from app.agent.tools.base import Tool, ToolRegistry
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import StreamEvent, ToolCall, Usage
from app.services.answer_kind import (
    classify,
    clean_citations,
    is_chitchat,
    last_user_text,
    sounds_no_info,
    wants_human,
)
from app.services.knowledge_gaps import gap_kind, group_questions

# ----------------------------------------------------------------- classification


def test_chitchat_and_human_detection():
    for t in ("你好", "在吗？", "谢谢！", "1", "ok", "", "   ", "嗯嗯", "好的好的"):
        assert is_chitchat(t), t
    for t in ("现在用不了gpt", "6.1", "E1001", "退款", "卡密在哪里"):
        assert not is_chitchat(t), t
    assert wants_human("转人工") and wants_human("人工客服在吗") and wants_human("找个真人")
    assert not wants_human("人工智能客服系统怎么接入到我自己的网站里面去呢")


def test_sounds_no_info():
    assert sounds_no_info("目前知识库里没有查到「模型广场」的具体清单")
    assert sounds_no_info("资料中没有说明这一点")
    assert not sounds_no_info("七天内可以无理由退货。")


def test_clean_citations_keeps_only_referenced_refs():
    cites = [{"ref": 1, "title": "A"}, {"ref": 2, "title": "B"}, {"ref": 3, "title": "C"}]
    assert clean_citations("按 [来源2] 的规定可以退。[来源3]", cites) == cites[1:]
    assert clean_citations("知识库里没有相关资料，建议联系客服。", cites) == []
    assert clean_citations("可以退，七天内。", cites) == cites  # confident, un-numbered → keep all
    assert clean_citations("[来源1]", []) == []


def test_classify_priority_and_kinds():
    base = dict(user_text="买错了可以退款吗", final_text="可以。[来源1]", citations=[{"ref": 1}], searched=True)
    assert classify(**base) == "grounded"
    assert classify(**{**base, "degraded": True}) == "degraded"
    assert classify(**{**base, "off_topic": True}) == "off_topic"
    assert classify(**{**base, "escalated": True}) == "handoff"
    assert classify(**{**base, "user_text": "你好"}) == "chitchat"
    assert classify(**{**base, "user_text": "转人工"}) == "handoff"
    assert classify(**{**base, "final_text": "知识库里没有相关资料。", "citations": []}) == "no_info"
    assert classify(**{**base, "citations": [], "searched": True, "final_text": "大概是网络问题。"}) == "no_info"
    assert classify(**{**base, "citations": [], "searched": False, "final_text": "大概是网络问题。"}) == "general"


def test_last_user_text_handles_parts():
    msgs = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": [{"type": "text", "text": "这是什么"}, {"type": "image_url", "image_url": {"url": "u"}}]},
    ]
    assert last_user_text(msgs) == "这是什么"
    assert last_user_text([]) == ""


# ----------------------------------------------------------------- gaps


def test_gap_kind_buckets():
    assert gap_kind("no_info", False, True) == "no_info"
    assert gap_kind("grounded", True, True) == "miss"  # retrieval missed once, model recovered
    assert gap_kind("general", False, False) == "unsearched"
    assert gap_kind("grounded", False, True) is None
    assert gap_kind("", False, False) is None  # legacy, unknown
    assert gap_kind("", True, True) == "miss"  # legacy but the trace says it missed


def _row(q, kind="miss", ts=None, sid="s1"):
    return {"question": q, "kind": kind, "created_at": ts or datetime(2026, 11, 4, tzinfo=UTC), "session_id": sid}


def test_group_questions_merges_rephrasings_and_skips_noise():
    rows = [
        _row("为什么现在没有gpt6.1", "miss"),
        _row("GPT6.1 怎么没有了", "no_info"),
        _row("卡密在哪里查看", "miss"),
        _row("你好"),  # chitchat → dropped
        _row("转人工"),  # handoff → dropped
        _row("卡密在哪里查看", "unsearched"),
    ]
    groups = group_questions(rows)
    assert [g["count"] for g in groups] == [2, 2]
    by_q = {g["question"]: g for g in groups}
    assert by_q["为什么现在没有gpt6.1"]["kinds"] == {"miss": 1, "no_info": 1}
    assert by_q["卡密在哪里查看"]["kinds"] == {"miss": 1, "unsearched": 1}
    assert len(by_q["为什么现在没有gpt6.1"]["examples"]) == 2


def test_group_questions_honours_ignores_fuzzily():
    rows = [_row("卡密在哪里查看"), _row("卡密在哪查看呢"), _row("订单号在哪里找")]
    groups = group_questions(rows, ignored=["卡密在哪里查看"])
    assert [g["question"] for g in groups] == ["订单号在哪里找"]


# ----------------------------------------------------------------- runner behaviour


class Scripted(LLMProvider):
    def __init__(self, turns):
        super().__init__(LLMSettings("fake", "", "", "fake-model"))
        self.turns, self.calls, self.tool_choices = turns, 0, []

    async def stream_chat(self, messages, tools=None, tool_choice="auto", **kw):
        self.tool_choices.append(tool_choice if tools else None)
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        for ev in turn:
            yield ev

    async def complete(self, messages, **kw):
        return StreamEvent(kind="done", usage=Usage(1, 1))


class FakeSearch(Tool):
    name = "search_knowledge"
    description = "kb"

    async def run(self, args, ctx):
        ctx.citations.append({"ref": 1, "item_id": "i1", "chunk_id": "c1", "title": "退款政策", "score": 0.3})
        return "[来源1] 退款政策\n虚拟商品除质量问题外概不退款。"


@pytest.fixture
def run(monkeypatch):
    persisted: dict = {}
    state = {"user": "买错了可以退款吗"}

    async def _build(*a, **k):
        return [{"role": "system", "content": "s"}, {"role": "user", "content": state["user"]}]

    async def _persist(self, db, session, text, trace, citations, trace_id, usage, cost, lat, degraded,
                       answer_kind=""):
        persisted.update(text=text, degraded=degraded, citations=citations, answer_kind=answer_kind,
                         tools=len(trace))
        return SimpleNamespace(id="m1")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(runner_mod, "build_messages", _build)
    monkeypatch.setattr(runner_mod, "needs_summarization", lambda s: False)
    monkeypatch.setattr(runner_mod, "record_turn_cost", _noop)
    monkeypatch.setattr(AgentRunner, "_persist_assistant", _persist)
    reg = ToolRegistry()
    reg.register(FakeSearch())
    cfg = SimpleNamespace(llm_model="fake-model", guard_enabled=False)

    async def _run(turns, user="买错了可以退款吗"):
        state["user"] = user
        provider = Scripted(turns)
        r = AgentRunner(provider, reg, cfg)
        events = [ev async for ev in r.run_turn(None, SimpleNamespace(id="s1"), "t")]
        return events, persisted, provider

    return _run


SEARCH = [
    StreamEvent(kind="tool_calls", tool_calls=[ToolCall(id="c1", name="search_knowledge", arguments='{"query":"退款"}')]),
    StreamEvent(kind="done", usage=Usage(5, 0), finish_reason="tool_calls"),
]


def _answer(text):
    return [StreamEvent(kind="text", text=text), StreamEvent(kind="done", usage=Usage(5, 5), finish_reason="stop")]


@pytest.mark.asyncio
async def test_first_pass_forces_search_and_grounded_answer_keeps_cited(run):
    events, persisted, provider = await run([SEARCH, _answer("除质量问题外不能退款。[来源1]")])
    assert provider.tool_choices[0] == {"type": "function", "function": {"name": "search_knowledge"}}
    assert provider.tool_choices[1] == "auto"
    assert persisted["answer_kind"] == "grounded"
    assert [c["ref"] for c in persisted["citations"]] == [1]
    assert events[-1].data["answer_kind"] == "grounded"


@pytest.mark.asyncio
async def test_no_info_answer_drops_citations_and_tells_client(run):
    events, persisted, _ = await run([SEARCH, _answer("知识库里暂时没有相关资料，建议联系客服。")])
    assert persisted["answer_kind"] == "no_info"
    assert persisted["citations"] == []
    # the streamed full list is corrected by a trailing empty citations event
    cit_events = [e for e in events if e.kind == "citations"]
    assert cit_events and cit_events[-1].citations == []
    assert events[-1].data["citations"] == []


@pytest.mark.asyncio
async def test_greeting_and_handoff_requests_are_not_forced_to_search(run):
    _, persisted, provider = await run([_answer("你好！有什么可以帮你？")], user="你好")
    assert provider.tool_choices == ["auto"]
    assert persisted["answer_kind"] == "chitchat"
    _, persisted, provider = await run([_answer("好的，马上为你转接。")], user="转人工")
    assert provider.tool_choices == ["auto"]
    assert persisted["answer_kind"] == "handoff"


@pytest.mark.asyncio
async def test_empty_reply_fallback_is_degraded(run):
    empty = [StreamEvent(kind="done", usage=Usage(1, 0), finish_reason="stop")]
    _, persisted, _ = await run([SEARCH, empty, empty])
    assert persisted["degraded"] is True
    assert persisted["answer_kind"] == "degraded"
    assert "没能正常生成回复" in persisted["text"]
