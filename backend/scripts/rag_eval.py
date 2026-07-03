"""RAG retrieval evaluation harness (requirements §15).

Seeds a small labelled corpus, runs hybrid retrieval for each query, and reports
recall@k / MRR for POSITIVE cases plus a correct-rejection rate for NEGATIVE cases —
questions the KB can't answer, which SHOULD return nothing so the agent escalates
instead of answering from weak context. Runs against the keyword path with no embedding
key required; with embeddings + rerank configured it also exercises those paths.

The built-in lists are a smoke set. Real domain cases live in a JSONL file (--cases,
see scripts/eval_cases.example.jsonl), one object per line:

    {"type": "corpus",   "title": "...", "content": "..."}     # replaces built-in corpus
    {"type": "positive", "query": "...", "expected_title": "..."}
    {"type": "negative", "query": "..."}

Lines starting with '#' are comments. A positive whose expected_title is empty is
skipped with a notice — that is the labelling queue scripts/mine_bad_cases.py mines
from live 👎/handoff traffic; label it, then it counts.

Safety: this seeds *published* items into whatever DB the environment points at, so
every seeded title carries a per-run tag AND category='eval'; cleanup runs in a
finally block and also sweeps leftovers from a previously crashed run. Still, prefer
an isolated eval database when one is at hand.

Usage:  python -m scripts.rag_eval [--cases path/to/cases.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from sqlalchemy import delete, or_

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeItem
from app.rag.retrieval import hybrid_search
from app.services.ai_config import (
    get_active_ai_config,
    to_embedding_settings,
    to_rerank_settings,
)
from app.services.knowledge import reembed_item

log = get_logger("rag_eval")

EVAL_CATEGORY = "eval"
_TITLE_TAG = "[rag-eval-"  # every seeded title starts with this + a per-run id

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


def load_cases(path: str) -> tuple[list, list, list]:
    """(corpus, positives, negatives) from a JSONL file.

    Sections fall back to the built-in lists independently, so a file that only adds
    real positive/negative queries keeps the built-in corpus."""
    corpus: list[tuple[str, str]] = []
    positives: list[tuple[str, str]] = []
    negatives: list[str] = []
    unlabelled = 0
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: 非法 JSON — {exc}") from exc
        kind = obj.get("type")
        if kind == "corpus":
            corpus.append((obj["title"], obj["content"]))
        elif kind == "positive":
            if not (obj.get("expected_title") or "").strip():
                unlabelled += 1  # mined but not yet labelled — see scripts/mine_bad_cases
                continue
            positives.append((obj["query"], obj["expected_title"]))
        elif kind == "negative":
            negatives.append(obj["query"])
        else:
            raise SystemExit(
                f"{path}:{lineno}: 未知 type {kind!r}（可用 corpus / positive / negative）"
            )
    if unlabelled:
        print(f"skipped {unlabelled} unlabelled positive case(s) — 填上 expected_title 后才参与评测")
    return corpus or CORPUS, positives or POSITIVE_CASES, negatives or NEGATIVE_CASES


async def _seed(db, corpus: list[tuple[str, str]], run_tag: str) -> int:
    for title, content in corpus:
        item = KnowledgeItem(
            title=f"{run_tag} {title}", content=content,
            category=EVAL_CATEGORY, status="published",
        )
        db.add(item)
        await db.flush()
        await reembed_item(db, str(item.id))  # commits
    return len(corpus)


async def _cleanup(db) -> int:
    """Delete eval items by BOTH markers (category and title tag) — belt and braces,
    and the tag-less match also sweeps residue from runs whose id we no longer know."""
    res = await db.execute(
        delete(KnowledgeItem).where(
            or_(
                KnowledgeItem.category == EVAL_CATEGORY,
                KnowledgeItem.title.like(f"{_TITLE_TAG}%"),
            )
        )
    )
    return res.rowcount or 0


async def _metrics(db, cfg, positives, negatives) -> tuple[float, float, float]:
    """(recall@K, MRR, correct-rejection-rate) for the labelled cases under `cfg`."""
    hits, rr = 0, 0.0
    for query, expected in positives:
        results = await hybrid_search(db, cfg, query, top_k=TOP_K)
        rank = next((i + 1 for i, r in enumerate(results) if expected in (r.title or "")), 0)
        if rank:
            hits += 1
            rr += 1.0 / rank
    rejected = 0
    for query in negatives:
        if not await hybrid_search(db, cfg, query, top_k=TOP_K):
            rejected += 1
    return hits / len(positives), rr / len(positives), rejected / len(negatives)


async def _report(db, cfg, positives, negatives) -> None:
    hits, rr_sum = 0, 0.0
    print(f"\n{'query':<26} {'expected':<12} rank  top result")
    print("-" * 72)
    for query, expected in positives:
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
    for query in negatives:
        results = await hybrid_search(db, cfg, query, top_k=TOP_K)
        ok = len(results) == 0
        rejected += int(ok)
        flag = "✓" if ok else f"✗ {results[0].title or ''}"
        print(f"{query:<26} {len(results):<12} {flag}")

    np_, nn = len(positives), len(negatives)
    print("-" * 72)
    print(f"Recall@{TOP_K}: {hits}/{np_} = {hits / np_:.0%}   MRR: {rr_sum / np_:.3f}")
    print(f"Correct rejection: {rejected}/{nn} = {rejected / nn:.0%}")


# min_score values to sweep — the rerank precision/recall gate. Re-run as your KB grows to
# pick the frontier for YOUR data (highest reject rate that still holds recall@K at 100%).
SWEEP_MIN_SCORE = (0.0, 0.08, 0.10, 0.12, 0.15, 0.20)


async def _sweep_min_score(db, cfg, positives, negatives) -> None:
    # Mirror hybrid_search's own gate (enabled AND a usable key): min_score only filters
    # when a rerank actually happened, so sweeping it without one yields N identical rows
    # that read as "min_score is insensitive".
    rer = to_rerank_settings(cfg)
    if not (rer and rer.api_key):
        print(
            "\n--- min_score sweep: skipped — rerank 未配置"
            "（rerank_enabled=False 或缺 API key）。min_score 仅在 rerank 生效时参与过滤，"
            "此时扫描结果恒为同一行，无调参意义。"
        )
        return
    orig = dict(cfg.retrieval or {})
    # Score domains differ per rerank model, so a sweep is only valid for the named one.
    print(f"\n--- min_score sweep (rerank model: {rer.model} — 阈值按该模型标定，换模型需重扫) ---")
    print(f"{'min_score':>10} | {'recall@' + str(TOP_K):>9} | {'MRR':>6} | {'reject':>7}")
    print("-" * 42)
    rows = []
    for ms in SWEEP_MIN_SCORE:
        cfg.retrieval = {**orig, "min_score": ms}  # in-memory only; restored below
        rec, mrr, rej = await _metrics(db, cfg, positives, negatives)
        rows.append((rec, mrr, rej))
        print(f"{ms:>10.2f} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")
    cfg.retrieval = orig
    if len(set(rows)) == 1:
        print(
            "!! 各取值指标完全一致 — rerank 客户端可能在运行期降级（全零分回退融合序），"
            "本表不可用于调参。"
        )


SWEEP_VECTOR_MIN_SIM = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


async def _sweep_vector_min_sim(db, cfg, positives, negatives) -> None:
    """vector_min_sim × rerank on/off — the axes interact, so sweep them jointly.

    With rerank on, min_score is the calibrated final gate and the pre-gate mostly
    controls rerank cost (candidates rerank could have saved die here — a lower value
    may buy recall). Without rerank the pre-gate IS the relevance floor, so lowering
    it trades rejection for recall directly."""
    if not to_embedding_settings(cfg).api_key:
        print("\n--- vector_min_sim sweep: skipped — embedding 未配置，向量路径不参与检索。")
        return
    rer = to_rerank_settings(cfg)
    rerank_axis = (False, True) if (rer and rer.api_key) else (False,)
    if len(rerank_axis) == 1:
        print("\n(rerank 未配置 — vector_min_sim 只扫 rerank=off 一侧)")
    orig = dict(cfg.retrieval or {})
    orig_rerank = cfg.rerank_enabled
    print("\n--- vector_min_sim × rerank sweep ---")
    print(f"{'rerank':>7} | {'min_sim':>8} | {'recall@' + str(TOP_K):>9} | {'MRR':>6} | {'reject':>7}")
    print("-" * 52)
    try:
        for rerank_on in rerank_axis:
            cfg.rerank_enabled = rerank_on  # in-memory only; restored below
            for vms in SWEEP_VECTOR_MIN_SIM:
                cfg.retrieval = {**orig, "vector_min_sim": vms}
                rec, mrr, rej = await _metrics(db, cfg, positives, negatives)
                print(
                    f"{'on' if rerank_on else 'off':>7} | {vms:>8.2f} | "
                    f"{rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}"
                )
    finally:
        cfg.retrieval = orig
        cfg.rerank_enabled = orig_rerank


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RAG retrieval evaluation harness",
    )
    parser.add_argument(
        "--cases",
        help="JSONL 评测集（格式见 scripts/eval_cases.example.jsonl）；缺省用内置冒烟集",
    )
    return parser.parse_args()


async def main() -> None:
    configure_logging()
    args = _parse_args()
    corpus, positives, negatives = (
        load_cases(args.cases) if args.cases else (CORPUS, POSITIVE_CASES, NEGATIVE_CASES)
    )
    run_tag = f"[rag-eval-{uuid.uuid4().hex[:8]}]"

    async with session_scope() as db:
        # Sweep residue from a previously crashed run BEFORE seeding, so stale synthetic
        # items can't pollute this run's ranking (or linger in a live KB any longer).
        leftover = await _cleanup(db)
        await db.commit()
        if leftover:
            print(f"removed {leftover} leftover eval item(s) from a previous run")

        try:
            await _seed(db, corpus, run_tag)
            cfg = await get_active_ai_config(db)
            await _report(db, cfg, positives, negatives)
            await _sweep_min_score(db, cfg, positives, negatives)
            await _sweep_vector_min_sim(db, cfg, positives, negatives)
        finally:
            # rollback first: cleanup must run even when the failure aborted the tx
            # (seeding commits per item, so nothing of value is lost).
            await db.rollback()
            removed = await _cleanup(db)
            await db.commit()
            print(f"\ncleaned up {removed} seeded eval item(s).")


if __name__ == "__main__":
    asyncio.run(main())
