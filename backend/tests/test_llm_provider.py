"""Unit: OpenAI-compatible provider & embedding client — SSE parsing, tool-call
accumulation, error handling, asymmetric text_type passthrough, dimension guard.

Uses httpx.MockTransport so no network is touched. Guards the §2/§6.1 constraint that
the provider abstraction stays correct when swapping models/providers.
"""

import json

import httpx
import pytest

from app.llm.base import LLMSettings
from app.llm.embeddings import EmbeddingClient, EmbeddingDimensionMismatch, EmbeddingSettings
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


# ---- EmbeddingClient ----


def _emb_client(
    handler,
    model: str = "text-embedding-v4",
    base_url: str = "https://dashscope.example.com/compatible-mode/v1",
    dim: int = 4,
) -> EmbeddingClient:
    c = EmbeddingClient(EmbeddingSettings(base_url=base_url, api_key="k", model=model, dim=dim))
    c._client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handler))
    return c


def _emb_response(reqs: list[dict], dim: int = 4) -> httpx.Response:
    n = len(reqs[-1]["input"]) if isinstance(reqs[-1]["input"], list) else 1
    return httpx.Response(
        200, json={"data": [{"index": i, "embedding": [0.1] * dim} for i in range(n)]}
    )


@pytest.mark.asyncio
async def test_embed_passes_text_type_and_dimensions_to_dashscope():
    reqs: list[dict] = []

    def handler(req):
        reqs.append(json.loads(req.content))
        return _emb_response(reqs)

    c = _emb_client(handler)
    vec = await c.embed_one("退货政策", text_type="query")
    assert len(vec) == 4
    assert reqs[0]["text_type"] == "query" and reqs[0]["dimensions"] == 4

    await c.embed(["七天无理由退货。"])  # document side defaults to 'document'
    assert reqs[1]["text_type"] == "document"


@pytest.mark.asyncio
async def test_embed_omits_optional_fields_for_non_dashscope_provider():
    reqs: list[dict] = []

    def handler(req):
        reqs.append(json.loads(req.content))
        return _emb_response(reqs)

    c = _emb_client(handler, model="BAAI/bge-small-zh-v1.5", base_url="http://localhost:8100/v1")
    await c.embed_one("退货政策", text_type="query")
    assert "text_type" not in reqs[0] and "dimensions" not in reqs[0]


@pytest.mark.asyncio
async def test_embed_falls_back_to_bare_request_when_extras_rejected():
    reqs: list[dict] = []

    def handler(req):
        body = json.loads(req.content)
        reqs.append(body)
        if "text_type" in body or "dimensions" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field"}})
        return _emb_response(reqs)

    c = _emb_client(handler)
    out = await c.embed(["a"], text_type="query")
    assert len(out) == 1  # degraded, not failed
    assert len(reqs) == 2 and "text_type" not in reqs[1]
    # The rejection is remembered: later calls go bare directly, no doubled request.
    await c.embed(["b"])
    assert len(reqs) == 3 and "dimensions" not in reqs[2]


@pytest.mark.asyncio
async def test_dim_mismatch_raises_on_embed_and_empties_embed_one():
    reqs: list[dict] = []

    def handler(req):
        reqs.append(json.loads(req.content))
        return _emb_response(reqs, dim=8)  # provider returns 8-dim, config expects 4

    c = _emb_client(handler)
    with pytest.raises(EmbeddingDimensionMismatch):
        await c.embed(["doc"])  # reembed path: caller stores chunks unembedded (PENDING)
    assert len(reqs) == 1  # config error is not retried
    assert await c.embed_one("查询") == []  # query path: degrade to keyword-only


@pytest.mark.asyncio
async def test_embed_one_cache_is_keyed_by_text_type():
    reqs: list[dict] = []

    def handler(req):
        reqs.append(json.loads(req.content))
        return _emb_response(reqs)

    c = _emb_client(handler)
    await c.embed_one("退货政策", text_type="query")
    await c.embed_one("退货政策", text_type="query")  # cache hit
    assert len(reqs) == 1
    await c.embed_one("退货政策")  # 'document' vector differs on asymmetric providers
    assert len(reqs) == 2
