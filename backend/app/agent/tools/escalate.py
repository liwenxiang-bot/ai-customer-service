"""escalate_to_human — open a handoff ticket and notify the operator."""

from __future__ import annotations

from typing import Any

from app.agent.tools.base import Tool, ToolContext
from app.models.enums import HandoffReason
from app.services.handoff import create_handoff

_REASON_MAP = {
    "user_request": HandoffReason.USER_REQUEST,
    "cannot_resolve": HandoffReason.MODEL_DECISION,
    "negative_feedback": HandoffReason.NEGATIVE_FEEDBACK,
}


class EscalateToHumanTool(Tool):
    name = "escalate_to_human"
    description = (
        "当无法通过知识库解决用户问题、用户明确要求人工、或用户连续表达强烈不满时，"
        "调用本工具转接人工客服。调用后系统会建立工单并通知运营人员，你应告知用户"
        "「已为你转接人工，稍后会有同事跟进」。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["user_request", "cannot_resolve", "negative_feedback"],
                "description": "转人工原因。",
            },
            "summary": {
                "type": "string",
                "description": "用一两句话概括用户的问题与上下文，供人工客服快速了解。",
            },
        },
        "required": ["reason", "summary"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        reason = _REASON_MAP.get(args.get("reason", ""), HandoffReason.MODEL_DECISION)
        summary = (args.get("summary") or "").strip() or ctx.session.title or "（无摘要）"

        ticket = await create_handoff(
            ctx.db,
            ctx.session,
            reason=str(reason),
            reason_detail=args.get("reason", ""),
            summary=summary,
        )
        ctx.escalation = {
            "ticket_id": str(ticket.id),
            "reason": str(reason),
            "notified": ticket.notified,
        }
        return (
            "已成功创建转人工工单并通知运营人员。请用友好的语气告诉用户："
            "已经为他转接人工客服，稍后会有同事跟进；如方便可留下联系方式。"
        )
