"""Channel adapter abstraction.

A channel's only jobs are (1) normalize inbound traffic into an InboundMessage and
(2) render the agent's normalized AgentEvents into its own wire format. The Agent core
and ConversationService never learn which channel they're serving — adding Feishu/
DingTalk means adding an adapter, nothing else (requirements §2 约束).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from app.agent.events import AgentEvent
from app.models.enums import ChannelType


@dataclass
class InboundMessage:
    channel_type: str
    channel_key: str
    end_user_id: str
    text: str
    end_user_display: str = ""
    # session continuity hint (e.g. an existing session id the client holds)
    session_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # ip, user-agent, locale, ...
    attachments: list[dict[str, Any]] = field(default_factory=list)


class ChannelAdapter(abc.ABC):
    channel_type: ChannelType

    @abc.abstractmethod
    def render_event(self, ev: AgentEvent, **kw: Any) -> dict[str, Any] | None:
        """Translate one AgentEvent into this channel's outbound payload (or None to skip)."""
        raise NotImplementedError
