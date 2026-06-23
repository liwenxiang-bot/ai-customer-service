"""Optional Langfuse tracing hook (requirements §12).

Kept dependency-light: if `langfuse` isn't installed or LANGFUSE_ENABLED is false, every
function here is a no-op. The durable per-turn trace (tokens, cost, latency, tool calls,
citations, trace_id) always lives in the `messages` table + structured logs regardless;
Langfuse is an optional enhancement for a richer trace UI.

To enable: pip install langfuse, set LANGFUSE_ENABLED=true and the keys, then call
`trace_turn(...)` from the runner if you want spans in the Langfuse UI.
"""

from __future__ import annotations

from app.config import settings
from app.core.logging import get_logger

log = get_logger("tracing")

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse import Langfuse  # type: ignore

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        return _client
    except Exception as exc:  # noqa: BLE001
        log.warning("langfuse_init_skipped", error=str(exc))
        return None


def trace_turn(trace_id: str, *, session_id: str, model: str, input_text: str,
               output_text: str, prompt_tokens: int, completion_tokens: int,
               cost_usd: float, latency_ms: int, tool_calls: list, citations: list) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        trace = client.trace(id=trace_id, name="chat_turn", metadata={"session_id": session_id})
        trace.generation(
            name="llm", model=model, input=input_text, output=output_text,
            usage={"input": prompt_tokens, "output": completion_tokens},
            metadata={"cost_usd": cost_usd, "latency_ms": latency_ms,
                      "tool_calls": tool_calls, "citations": citations},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("langfuse_trace_skipped", error=str(exc))
