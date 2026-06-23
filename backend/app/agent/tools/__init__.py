"""Default tool registry. Register new tools here to make them available to the agent."""

from __future__ import annotations

from app.agent.tools.base import Tool, ToolContext, ToolRegistry
from app.agent.tools.escalate import EscalateToHumanTool
from app.agent.tools.get_order import GetOrderTool
from app.agent.tools.search_knowledge import SearchKnowledgeTool


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(SearchKnowledgeTool())
    reg.register(EscalateToHumanTool())
    reg.register(GetOrderTool())
    return reg


default_registry = build_default_registry()

__all__ = ["Tool", "ToolContext", "ToolRegistry", "build_default_registry", "default_registry"]
