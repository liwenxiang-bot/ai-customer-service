"""Tool framework: a small, extensible registry the AgentRunner exposes to the LLM.

Adding a business capability = subclass Tool + register it. The runner is agnostic to
which tools exist (requirements §6.2 — tool set must be extensible).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import ToolSchema
from app.models.config import AIConfig
from app.models.conversation import Session


@dataclass
class ToolContext:
    """Per-turn context handed to each tool. Tools also push structured side effects
    (citations, escalation) back onto it for the runner to surface to the client."""

    db: AsyncSession
    session: Session
    ai_config: AIConfig
    trace_id: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, Any] | None = None


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abc.abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        """Execute and return a STRING result that is fed back to the model.

        Must not raise for ordinary failures — return an error description so the model
        can decide how to respond (requirements §6.2 工具失败交回模型)."""
        raise NotImplementedError

    def schema(self) -> ToolSchema:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())
