"""Unit: OpenAI-compatible provider — SSE parsing, tool-call accumulation, error handling.

Uses httpx.MockTransport so no network is touched. Guards the §2/§6.1 constraint that
the provider abstraction stays correct when swapping models/providers.
"""

import httpx
import pytest

from app.llm.base import LLMSettings
from app.llm.openai_provider import OpenAICompatProvider


def _provider(handler) -> OpenAICompatProvider:
    p = OpenAICompatProvider(LLMSettings("openai", "http://x/v1", "k", "test-model"))
    p._client = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    return p


def _sse(*chunks: str) -> str:
    return "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_text_streaming_and_usage():
    body = _sse(
        '{"choices":[{"delta":{"content":"你好"}}]}',
        '{"choices":[{"delta":{"content":"，世界"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}',
    )
    p = _provider(lambda req: httpx.Response(200, text=body))
    texts, done = [], None
    async for ev in p.stream_chat([{"role": "user", "content": "hi"}]):
        if ev.kind == "text":
            texts.append(ev.text)
        elif ev.kind == "done":
            done = ev
    assert "".join(texts) == "你好，世界"
    assert done.usage.prompt_tokens == 10 and done.usage.completion_tokens == 5


@pytest.mark.asyncio
async def test_tool_call_accumulation():
    body = _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"search_knowledge","arguments":"{\\"qu"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ery\\":\\"退货\\"}"}}]}}]}',
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    )
    p = _provider(lambda req: httpx.Response(200, text=body))
    tool_calls = None
    async for ev in p.stream_chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}]):
        if ev.kind == "tool_calls":
            tool_calls = ev.tool_calls
    assert tool_calls and tool_calls[0].name == "search_knowledge"
    assert tool_calls[0].parsed_arguments() == {"query": "退货"}


@pytest.mark.asyncio
async def test_http_error_yields_error_event_not_exception():
    p = _provider(lambda req: httpx.Response(500, text="boom"))
    kinds = [ev.kind async for ev in p.stream_chat([{"role": "user", "content": "hi"}])]
    assert "error" in kinds  # degrades gracefully instead of raising
