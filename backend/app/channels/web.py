"""Web channel adapter — renders AgentEvents to the WebSocket wire protocol.

Outbound server→client event types:
  connected, message_start, stream_chunk, tool_status, message_end, escalation,
  error, pong
Inbound client→server: user_message, feedback, ping (handled in the WS endpoint).
"""

from __future__ import annotations

from typing import Any

from app.agent.events import AgentEvent
from app.channels.base import ChannelAdapter
from app.models.enums import ChannelType


class WebAdapter(ChannelAdapter):
    channel_type = ChannelType.WEB

    def render_event(self, ev: AgentEvent, **kw: Any) -> dict[str, Any] | None:
        if ev.kind == "text":
            return {"type": "stream_chunk", "delta": ev.text}
        if ev.kind == "tool_status":
            return {
                "type": "tool_status",
                "tool": ev.tool,
                "status": ev.status,
                "label": _tool_label(ev.tool, ev.status),
            }
        if ev.kind == "citations":
            return {"type": "citations", "citations": ev.citations}
        if ev.kind == "escalation":
            return {"type": "escalation", **ev.data}
        if ev.kind == "done":
            return {
                "type": "message_end",
                "message_id": ev.data.get("message_id"),
                "session_id": ev.data.get("session_id"),
                "citations": ev.data.get("citations", []),
                "escalation": ev.data.get("escalation"),
                "usage": ev.data.get("usage"),
                "degraded": ev.data.get("degraded", False),
            }
        if ev.kind == "error":
            return {"type": "error", "message": ev.error or "服务暂时不可用"}
        return None


def _tool_label(tool: str, status: str) -> str:
    if status == "running":
        return {
            "search_knowledge": "正在查询知识库…",
            "get_order": "正在查询订单…",
            "escalate_to_human": "正在为你转接人工…",
        }.get(tool, "正在处理…")
    return ""


web_adapter = WebAdapter()
