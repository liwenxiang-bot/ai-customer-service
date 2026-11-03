"""Default tool registry. Register new tools here to make them available to the agent."""

from __future__ import annotations

from app.agent.tools.base import Tool, ToolContext, ToolRegistry
from app.agent.tools.decline import DeclineOffTopicTool
from app.agent.tools.escalate import EscalateToHumanTool
from app.agent.tools.get_order import GetOrderTool
from app.agent.tools.search_knowledge import SearchKnowledgeTool
from app.config import settings


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(SearchKnowledgeTool())
    reg.register(EscalateToHumanTool())
    reg.register(DeclineOffTopicTool())  # exposed to the model only when the guard is on
    # get_order is a stub that returns fabricated tracking numbers / amounts. Never expose it
    # to real customers by default — opt in explicitly for demos until a real order backend
    # (with end-user ownership checks) is wired in.
    if settings.enable_demo_tools:
        reg.register(GetOrderTool())
    return reg


default_registry = build_default_registry()

__all__ = ["Tool", "ToolContext", "ToolRegistry", "build_default_registry", "default_registry"]
