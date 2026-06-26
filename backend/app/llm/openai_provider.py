"""OpenAI-compatible provider — raw HTTP (httpx), no vendor SDK.

Works against any endpoint speaking the OpenAI /chat/completions contract: OpenAI,
DeepSeek, 通义 (DashScope compat), GLM, Moonshot, local vLLM/Ollama, etc. This is the
single integration point the whole system funnels LLM traffic through.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger
from app.llm.base import LLMProvider, LLMSettings
from app.llm.types import Message, StreamEvent, ToolCall, ToolSchema, Usage

log = get_logger("llm.openai")

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 120.0


class OpenAICompatProvider(LLMProvider):
    def __init__(self, cfg: LLMSettings) -> None:
        super().__init__(cfg)
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ payload
    def _payload(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None,
        tool_choice: str | dict = "auto",
    ) -> dict:
        payload: dict = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ------------------------------------------------------------------ stream
    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | dict = "auto",
    ) -> AsyncIterator[StreamEvent]:
        payload = self._payload(messages, tools, True, temperature, max_tokens, tool_choice)
        # Accumulate tool-call fragments by index across delta chunks.
        tool_acc: dict[int, dict] = {}
        usage = Usage()
        finish_reason: str | None = None

        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise httpx.HTTPStatusError(
                        f"LLM {resp.status_code}: {body.decode('utf-8', 'ignore')[:500]}",
                        request=resp.request,
                        response=resp,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("usage"):
                        u = obj["usage"]
                        usage = Usage(
                            prompt_tokens=u.get("prompt_tokens", 0),
                            completion_tokens=u.get("completion_tokens", 0),
                        )

                    for choice in obj.get("choices", []):
                        delta = choice.get("delta", {})
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]

                        text = delta.get("content")
                        if text:
                            yield StreamEvent(kind="text", text=text)

                        for tc in delta.get("tool_calls", []) or []:
                            idx = tc.get("index", 0)
                            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            log.warning("llm_stream_error", error=str(exc), model=self.cfg.model)
            yield StreamEvent(kind="error", error=str(exc))
            return

        if tool_acc:
            calls = [
                ToolCall(id=v["id"] or f"call_{i}", name=v["name"], arguments=v["args"])
                for i, v in sorted(tool_acc.items())
                if v["name"]
            ]
            if calls:
                yield StreamEvent(kind="tool_calls", tool_calls=calls)

        yield StreamEvent(
            kind="done", usage=usage, finish_reason=finish_reason or "stop"
        )

    # ------------------------------------------------------------ non-streaming
    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> StreamEvent:
        payload = self._payload(messages, None, False, temperature, max_tokens)
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        obj = resp.json()
        choice = (obj.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        u = obj.get("usage") or {}
        return StreamEvent(
            kind="done",
            text=text,
            usage=Usage(
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason", "stop"),
        )
