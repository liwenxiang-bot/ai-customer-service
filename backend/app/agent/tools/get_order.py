"""get_order — representative business-system tool (stub).

Demonstrates the pattern for wiring real backends: timeout, graceful degradation,
and returning a model-readable string. Replace the body with a real API call.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools.base import Tool, ToolContext
from app.core.logging import get_logger

log = get_logger("tool.get_order")


class GetOrderTool(Tool):
    name = "get_order"
    description = (
        "根据订单号查询订单状态、物流与金额等信息。仅当用户提供了订单号时调用。"
        "（示例业务工具，当前返回模拟数据，对接真实系统时替换实现。）"
    )
    parameters = {
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "订单号。"},
        },
        "required": ["order_id"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        order_id = (args.get("order_id") or "").strip()
        if not order_id:
            return "请用户提供订单号。"
        # TODO: replace with a real, timeout-guarded call to the order system.
        log.info("get_order_stub", order_id=order_id)
        return (
            f"（模拟数据）订单 {order_id} 状态：已发货；"
            "物流：顺丰 SF1234567890，预计明天送达；金额：￥199.00。"
            "如需真实数据，请在 get_order 工具中对接订单系统。"
        )
