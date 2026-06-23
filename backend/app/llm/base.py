"""Abstract LLM provider + a resolved config snapshot.

The AgentRunner depends only on this interface, never on a concrete vendor. Switching
providers/models is a config change (DB-backed ai_configs), not a code change
(requirements §2, §6.1).
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.llm.types import Message, StreamEvent, ToolSchema


@dataclass(frozen=True)
class LLMSettings:
    """Everything needed to talk to one chat model. Built from the active AIConfig
    (or env fallback). Frozen + hashable so it can key a provider cache."""

    provider: str
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.3
    max_tokens: int = 1024


class LLMProvider(abc.ABC):
    def __init__(self, cfg: LLMSettings) -> None:
        self.cfg = cfg

    @abc.abstractmethod
    def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents (text deltas, then tool_calls/done)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> StreamEvent:
        """Non-streaming convenience: returns a single 'done' event with full text.
        Used for summarization, knowledge distillation, etc."""
        raise NotImplementedError
