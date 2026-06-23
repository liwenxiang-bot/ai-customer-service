"""RAG retrieval evaluation harness (requirements §15).

Seeds a small labelled corpus, runs hybrid retrieval for each query, and reports
recall@k / MRR so retrieval quality can be regression-tested as the pipeline evolves.
Runs against the keyword path with no embedding key required; with embeddings
configured it also exercises the vector path.

Usage:  python -m scripts.rag_eval
Extend EVAL_CASES with real-world queries from your domain to grow the set.
"""

from __future__ import annotations

import asyncio

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeItem
from app.rag.retrieval import hybrid_search
from app.services.ai_config import get_active_ai_config
from app.services.knowledge import reembed_item
from sqlalchemy import delete

log = get_logger("rag_eval")

# (title, content) corpus
CORPUS = [
    ("退货政策", "本店支持7天无理由退货，商品需保持完好，不影响二次销售。"),
    ("退款时效", "退款将在收到退货后3个工作日内原路返回到您的支付账户。"),
    ("发货时间", "现货商品在付款后48小时内发出，预售商品以页面标注为准。"),
    ("错误码E1001", "错误码 E1001 表示退货申请已超过有效期，请联系人工客服处理。"),
    ("会员权益", "黄金会员享受免运费、专属客服与生日礼券等权益。"),
]

# (query, expected_title_substring)
EVAL_CASES = [
    ("怎么退货", "退货政策"),
    ("多久能退款到账", "退款时效"),
    ("E1001 是什么意思", "错误码E1001"),
    ("什么时候发货", "发货时间"),
    ("会员有什么好处", "会员权益"),
]

TOP_K = 3


async def _seed(db) -> list[str]:
    ids = []
    for title, content in CORPUS:
        item = KnowledgeItem(title=title, content=content, category="eval", status="published")
        db.add(item)
        await db.flush()
        await reembed_item(db, str(item.id))  # commits
        ids.append(str(item.id))
    return ids


async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        ids = await _seed(db)
        cfg = await get_active_ai_config(db)

        hits, rr_sum = 0, 0.0
        print(f"\n{'query':<22} {'expected':<12} rank  result")
        print("-" * 64)
        for query, expected in EVAL_CASES:
            results = await hybrid_search(db, cfg, query, top_k=TOP_K)
            rank = next((i + 1 for i, r in enumerate(results) if expected in (r.title or "")), 0)
            if rank:
                hits += 1
                rr_sum += 1.0 / rank
            top = results[0].title if results else "—"
            print(f"{query:<22} {expected:<12} {rank or '—':<5} {top}")

        n = len(EVAL_CASES)
        print("-" * 64)
        print(f"Recall@{TOP_K}: {hits}/{n} = {hits / n:.0%}   MRR: {rr_sum / n:.3f}\n")

        # Cleanup seeded corpus.
        await db.execute(delete(KnowledgeItem).where(KnowledgeItem.id.in_([__import__('uuid').UUID(i) for i in ids])))
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
