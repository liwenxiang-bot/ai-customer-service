"""RAG retrieval evaluation harness (requirements §15).

Seeds a small labelled corpus, runs hybrid retrieval for each query, and reports
recall@k / MRR for POSITIVE cases plus a correct-rejection rate for NEGATIVE cases —
questions the KB can't answer, which SHOULD return nothing so the agent escalates
instead of answering from weak context. Runs against the keyword path with no embedding
key required; with embeddings + rerank configured it also exercises those paths.

Usage:  python -m scripts.rag_eval
Extend the case lists with real queries from your domain to grow the set.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeItem
from app.rag.retrieval import hybrid_search
from app.services.ai_config import get_active_ai_config
from app.services.knowledge import reembed_item

log = get_logger("rag_eval")

# (title, content) corpus
CORPUS = [
    ("退货政策", "本店支持7天无理由退货，商品需保持完好，不影响二次销售。"),
    ("退款时效", "退款将在收到退货后3个工作日内原路返回到您的支付账户。"),
    ("发货时间", "现货商品在付款后48小时内发出，预售商品以页面标注为准。"),
    ("错误码E1001", "错误码 E1001 表示退货申请已超过有效期，请联系人工客服处理。"),
    ("会员权益", "黄金会员享受免运费、专属客服与生日礼券等权益。"),
    ("发票申请", "支持开具增值税电子普通发票，下单后7天内可在订单页申请。"),
]

# (query, expected_title_substring) — the KB should surface the labelled item.
POSITIVE_CASES = [
    ("怎么退货", "退货政策"),
    ("退货需要满足什么条件", "退货政策"),
    ("多久能退款到账", "退款时效"),
    ("E1001 是什么意思", "错误码E1001"),
    ("什么时候发货", "发货时间"),
    ("预售商品什么时候发货", "发货时间"),
    ("会员有什么好处", "会员权益"),
    ("能开发票吗", "发票申请"),
]

# Questions the corpus genuinely can't answer — retrieval SHOULD return nothing.
NEGATIVE_CASES = [
    "今天天气怎么样",
    "你们公司的股票代码是多少",
    "怎么注册一个新的社交账号",
    "帮我写一首关于春天的诗",
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


async def _metrics(db, cfg) -> tuple[float, float, float]:
    """(recall@K, MRR, correct-rejection-rate) for the labelled cases under `cfg`."""
    hits, rr = 0, 0.0
    for query, expected in POSITIVE_CASES:
        results = await hybrid_search(db, cfg, query, top_k=TOP_K)
        rank = next((i + 1 for i, r in enumerate(results) if expected in (r.title or "")), 0)
        if rank:
            hits += 1
            rr += 1.0 / rank
    rejected = 0
    for query in NEGATIVE_CASES:
        if not await hybrid_search(db, cfg, query, top_k=TOP_K):
            rejected += 1
    return hits / len(POSITIVE_CASES), rr / len(POSITIVE_CASES), rejected / len(NEGATIVE_CASES)


# min_score values to sweep — the rerank precision/recall gate. Re-run as your KB grows to
# pick the frontier for YOUR data (highest reject rate that still holds recall@K at 100%).
SWEEP_MIN_SCORE = (0.0, 0.08, 0.10, 0.12, 0.15, 0.20)


async def _sweep_min_score(db, cfg) -> None:
    orig = dict(cfg.retrieval or {})
    print(f"\n--- min_score sweep ---\n{'min_score':>10} | {'recall@' + str(TOP_K):>9} | {'MRR':>6} | {'reject':>7}")
    print("-" * 42)
    for ms in SWEEP_MIN_SCORE:
        cfg.retrieval = {**orig, "min_score": ms}  # in-memory only; never committed
        rec, mrr, rej = await _metrics(db, cfg)
        print(f"{ms:>10.2f} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")
    cfg.retrieval = orig


async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        ids = await _seed(db)
        cfg = await get_active_ai_config(db)

        hits, rr_sum = 0, 0.0
        print(f"\n{'query':<26} {'expected':<12} rank  top result")
        print("-" * 72)
        for query, expected in POSITIVE_CASES:
            results = await hybrid_search(db, cfg, query, top_k=TOP_K)
            rank = next((i + 1 for i, r in enumerate(results) if expected in (r.title or "")), 0)
            if rank:
                hits += 1
                rr_sum += 1.0 / rank
            top = results[0].title if results else "—"
            print(f"{query:<26} {expected:<12} {rank or '—':<5} {top}")

        print("-" * 72)
        print(f"{'negative query':<26} {'returned':<12} correct?")
        print("-" * 72)
        rejected = 0
        for query in NEGATIVE_CASES:
            results = await hybrid_search(db, cfg, query, top_k=TOP_K)
            ok = len(results) == 0
            rejected += int(ok)
            flag = "✓" if ok else f"✗ {results[0].title or ''}"
            print(f"{query:<26} {len(results):<12} {flag}")

        np_, nn = len(POSITIVE_CASES), len(NEGATIVE_CASES)
        print("-" * 72)
        print(f"Recall@{TOP_K}: {hits}/{np_} = {hits / np_:.0%}   MRR: {rr_sum / np_:.3f}")
        print(f"Correct rejection: {rejected}/{nn} = {rejected / nn:.0%}")

        await _sweep_min_score(db, cfg)

        # Cleanup seeded corpus.
        await db.execute(
            delete(KnowledgeItem).where(KnowledgeItem.id.in_([uuid.UUID(i) for i in ids]))
        )
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
