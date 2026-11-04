"""search_knowledge — the RAG entry point and primary tool.

Beyond running hybrid_search, this tool owns three prompt-side concerns:

- Miss guidance: the first empty recall in a turn asks the model to rewrite the query
  and retry once; the second falls back to "answer from general knowledge, escalate
  only per the system-prompt whitelist" so the tool copy never contradicts rules 2/5
  of the default system prompt.
- Citation continuity: [来源N] refs are unique across calls within a turn — a chunk
  already injected earlier keeps its ref and its full text is not injected twice.
- Token budget: injected context is capped by retrieval.context_token_budget; blocks
  past the budget degrade to title + snippet (or are dropped), best-ranked first.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import zip_longest
from typing import Any

from app.agent.context import estimate_tokens
from app.agent.tools.base import Tool, ToolContext
from app.core.logging import get_logger
from app.rag.retrieval import hybrid_search
from app.services.abuse_guard import guard_on
from app.services.ai_config import merged_retrieval

log = get_logger("tool.search_knowledge")

# Degraded blocks reuse the citation snippet length so no new data shape is introduced.
_SNIPPET_CHARS = 200


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
                    "要检索的问题，保留用户原话的自然问句，只做两件事：把指代词"
                    "（这个/那款/它/上面说的）替换为具体名称、补全省略的主体。保留用户"
                    "原话中的关键术语、产品型号、错误码；不要堆砌关键词或改写成长串。"
                    "例如上文在说“X1 手机”、用户接着问“它防水吗”，应检索“X1 手机防水吗”。"
                ),
            }
        },
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "未提供检索关键词。"

        # ToolContext lives for exactly one turn (runner.run_turn creates it once), so
        # per-turn counters can be stashed on the instance. getattr keeps this robust
        # whether or not the field is ever declared on the dataclass.
        attempt = getattr(ctx, "rag_search_calls", 0) + 1
        ctx.rag_search_calls = attempt

        results = await hybrid_search(ctx.db, ctx.ai_config, query)
        # Dual query on the first attempt: the customer's own words next to the model's
        # rewrite. A rewrite that drifts ("卡密在哪" → "充值卡 卡密 获取 查看 购买后")
        # still lands when the raw message matches; costs one cached embedding.
        raw = (getattr(ctx, "user_text", "") or "").strip()
        if attempt == 1 and raw and len(raw) <= 200 and _norm(raw) != _norm(query):
            try:
                extra = await hybrid_search(ctx.db, ctx.ai_config, raw)
            except Exception as exc:  # noqa: BLE001 — the rewrite's results stand on their own
                log.warning("dual_query_degraded", error=str(exc))
                extra = []
            if extra:
                top_k = int(merged_retrieval(ctx.ai_config)["top_k"])
                results = _merge_results(results, extra, top_k)
        if not results:
            misses = getattr(ctx, "rag_search_misses", 0) + 1
            ctx.rag_search_misses = misses
            # Covers both true misses and candidates filtered below the relevance
            # floor — the distinction only exists inside hybrid_search. The query text
            # here is the raw material for spotting recall gaps in the KB.
            log.info("knowledge_miss", query=query, attempt=attempt, misses=misses)
            decline_hint = (
                "若这个问题明显不属于本店业务范围（写代码、写作业、闲聊等），"
                "请直接调用 decline_off_topic，不要再检索。"
                if guard_on(ctx.ai_config) else ""
            )
            if misses == 1:
                return (
                    f"检索「{query}」未在知识库中命中。请换一种表述立即再检索一次，"
                    "不要原样重发相同的查询：把指代词补全为具体的产品/业务名称、"
                    "换用同义词、去掉语气词和口语词，或只保留最核心的关键词。" + decline_hint
                )
            return (
                f"检索「{query}」仍未命中，请不要再检索。请基于通用常识谨慎、友好地"
                "回应用户或追问澄清，涉及本店具体信息时说明「以商家实际为准」；"
                "不要编造具体的入口、路径、价格或政策。"
                "仅当用户明确要求人工、连续表达不满、或涉及账户操作/投诉/退款审批等"
                "必须人工处理的事项时，才调用 escalate_to_human 转人工。" + decline_hint
            )

        # [来源N] refs stay unique across calls in this turn: reuse the ref of a chunk
        # already cited (without re-injecting its text) and continue numbering after
        # the highest ref handed out so far. ctx.citations is the source of truth, so
        # the client-facing citation list stays free of duplicates too.
        known_refs = {c["chunk_id"]: c["ref"] for c in ctx.citations}
        next_ref = max((c["ref"] for c in ctx.citations), default=0) + 1

        budget = int(merged_retrieval(ctx.ai_config)["context_token_budget"])
        used = 0
        blocks: list[str] = []
        truncated = 0
        dropped = 0

        for r in results:
            title = r.title or "（无标题）"

            if r.chunk_id in known_refs:
                # Same chunk hit again: point the model at the existing block instead
                # of paying for the text twice.
                block = f"[来源{known_refs[r.chunk_id]}] {title}（本次检索同样命中，内容已在上文给出）"
                cost = estimate_tokens(block)
                if used + cost > budget and blocks:
                    dropped += 1
                    continue
                blocks.append(block)
                used += cost
                continue

            ref = next_ref
            block = f"[来源{ref}] {title}\n{r.context or r.content}"
            cost = estimate_tokens(block)
            if used + cost > budget:
                # Over budget: degrade to title + the citation snippet. Only drop the
                # result entirely if even that doesn't fit (never drop the first block,
                # otherwise the tool would return an empty result on a hit).
                block = f"[来源{ref}] {title}\n{r.content[:_SNIPPET_CHARS]}"
                cost = estimate_tokens(block)
                if used + cost > budget and blocks:
                    dropped += 1
                    continue
                truncated += 1

            next_ref += 1
            known_refs[r.chunk_id] = ref
            ctx.citations.append(
                {
                    "ref": ref,
                    "item_id": r.item_id,
                    "chunk_id": r.chunk_id,
                    "title": r.title,
                    "score": r.score,
                    "score_kind": getattr(r, "score_kind", "fusion"),
                    "snippet": r.content[:_SNIPPET_CHARS],
                }
            )
            blocks.append(block)
            used += cost

        log.info(
            "knowledge_retrieved",
            query=query,
            hits=len(results),
            top_score=results[0].score,
            attempt=attempt,
            injected=len(blocks),
            truncated=truncated,
            dropped=dropped,
            context_tokens=used,
        )
        return (
            "以下是检索到的资料。只有当资料直接回答了用户的问题时，才据此作答并在句末"
            "标注用到的[来源N]；如果资料只是沾边、没有直接回答，请明确告诉用户知识库暂无"
            "相关资料，不要标注引用，也不要猜测具体的入口、路径、价格或政策。\n\n"
            + "\n\n---\n\n".join(blocks)
        )


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", text or "")).lower()


def _merge_results(primary: list, extra: list, top_k: int) -> list:
    """Interleave two ranked result lists (rewrite first), dedupe by chunk, keep top_k.

    Rank-interleaving is the scale-free way to merge: fusion and rerank scores from two
    different queries are not comparable, ranks are."""
    out: list = []
    seen: set[str] = set()
    for pair in zip_longest(primary, extra):
        for r in pair:
            if r is not None and r.chunk_id not in seen:
                seen.add(r.chunk_id)
                out.append(r)
    return out[: max(top_k, 1)]
