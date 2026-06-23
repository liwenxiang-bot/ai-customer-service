"""Normalized LLM types — provider-agnostic. Business code never imports a vendor SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolCall:
    """A model-requested tool call (normalized from OpenAI's function-call format)."""

    id: str
    name: str
    arguments: str = ""  # raw JSON string as emitted by the model

    def parsed_arguments(self) -> dict[str, Any]:
        import json

        try:
            return json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}


@dataclass
class StreamEvent:
    """One event in a streamed completion.

    kind:
      - "text"       -> incremental assistant text (`text`)
      - "tool_calls" -> model finished and wants tools (`tool_calls`)
      - "done"       -> stream finished (`finish_reason`, `usage`)
      - "error"      -> provider error (`error`)
    """

    kind: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    error: str | None = None


# OpenAI-style message dict: {"role", "content", optional "tool_calls", "tool_call_id", "name"}
Message = dict[str, Any]
# OpenAI-style tool schema: {"type": "function", "function": {"name", "description", "parameters"}}
ToolSchema = dict[str, Any]
