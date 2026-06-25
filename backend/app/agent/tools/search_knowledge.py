"""search_knowledge — the RAG entry point and primary tool."""

from __future__ import annotations

from typing import Any

from app.agent.tools.base import Tool, ToolContext
from app.core.logging import get_logger
from app.rag.retrieval import hybrid_search

log = get_logger("tool.search_knowledge")


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = (
        "检索企业知识库，获取产品、政策、操作步骤、常见问题等权威资料。"
        "回答任何与业务相关的问题前都应先调用本工具。返回的资料带有[来源N]编号，"
        "请在回答中引用对应编号。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "要检索的查询语句，必须【自包含、可独立理解】。结合上文把指代词"
                    "（这个/那款/它/上面说的）替换为具体名称、补全省略的主体；保留用户"
                    "原话中的关键术语、产品型号、错误码。例如上文在说“X1 手机”、用户接着"
                    "问“它防水吗”，应检索“X1 手机 是否防水”，而不是“它防水吗”。"
                ),
            }
        },
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "未提供检索关键词。"

        results = await hybrid_search(ctx.db, ctx.ai_config, query)
        if not results:
            return (
                "知识库中未检索到相关资料。请根据你的通用知识谨慎回答；"
                "若问题需要权威或专属信息，请调用 escalate_to_human 转人工。"
            )

        # Record citations for the runner to attach to the message + send to the client.
        blocks: list[str] = []
        for i, r in enumerate(results, start=1):
            ctx.citations.append(
                {
                    "ref": i,
                    "item_id": r.item_id,
                    "chunk_id": r.chunk_id,
                    "title": r.title,
                    "score": r.score,
                    "snippet": r.content[:200],
                }
            )
            title = r.title or "（无标题）"
            blocks.append(f"[来源{i}] {title}\n{r.context or r.content}")

        log.info("knowledge_retrieved", query=query, hits=len(results))
        return (
            "以下是检索到的资料，请据此回答并标注引用的[来源N]：\n\n"
            + "\n\n---\n\n".join(blocks)
        )
