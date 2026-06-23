"""Normalized agent output events. Channels translate these into their wire protocol
(e.g. the Web WebSocket protocol), so the agent core stays channel-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    kind: str  # text | tool_status | citations | escalation | done | error
    text: str = ""
    tool: str = ""
    status: str = ""  # for tool_status: running | done | failed
    citations: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
