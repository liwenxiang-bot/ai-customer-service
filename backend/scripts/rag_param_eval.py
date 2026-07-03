"""Parameter-sweep eval for RAG retrieval.

Unlike scripts/rag_eval (a tiny smoke set), this synthesizes a *hard* corpus —
semantically-close clusters (退款时效 vs 退款方式 …) + long multi-section docs + noise —
so parameter differences actually surface. Beyond chunk_size and HNSW ef_construction
(both expensive: re-embed / index rebuild), it sweeps the cheap retrieval-time params
that gate recall in production — fusion weights, trgm_threshold, vector_min_sim,
candidate_multiplier, RRF K — and reports vector-only vs keyword-only recall so the
fusion weights have a direction, not just a score. Every table carries a
correct-rejection column: recall-only sweeps of a floor like vector_min_sim would
always conclude "lower is better".

Run:  python -m scripts.rag_param_eval
Heavy on the embedding API (re-embeds the corpus once per chunk_size). Marked items use
category='__param_eval__'; cleanup (items + index restore) runs in a finally block and
leftovers from a crashed run are swept on startup.
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.rag.retrieval import hybrid_search
from app.services.ai_config import get_active_ai_config, merged_retrieval

log = get_logger("rag_param_eval")
TAG = "__param_eval__"
TOP_K = 3

# Baseline build parameter of ix_chunks_embedding_hnsw — keep in sync with alembic
# 0015 and embedding_rebuild._alter_vector_dim, or a finished eval run silently
# downgrades the production index.
_DEFAULT_EF = 200

# ---- Ambiguous clusters: items a query could easily confuse (the hard part) ----
CLUSTERS: list[tuple[str, str]] = [
    ("退款到账时效", "退款审核通过后，款项将在1-3个工作日内原路退回到原支付账户，银行卡可能延迟到次日。"),
    ("退款方式说明", "退款仅支持原路返回，不支持更换收款账户或提现到余额，微信支付退回微信，支付宝退回支付宝。"),
    ("退款申请条件", "未发货可随时申请退款；已发货需先退货签收后才会退款；定制商品不支持退款。"),
    ("退款进度查询", "在『我的订单-退款/售后』中可查看退款进度，状态包括待审核、退款中、已到账。"),
    ("退款失败处理", "若退款失败多为原卡注销或账户异常，请在售后页提交新的收款信息由人工重新打款。"),
    ("退货邮费谁出", "七天无理由退货邮费由买家承担；商品质量问题或发错货导致的退货邮费由商家承担。"),
    ("退货操作步骤", "申请退货→等待审核→寄回商品并填写运单号→商家签收验货→退款，请保留寄回凭证。"),
    ("换货政策说明", "换货需商品未使用且不影响二次销售，同款不同规格可换，换货往返邮费规则同退货。"),
    ("发货时效说明", "现货付款后48小时内发出，预售商品以详情页标注时间为准，大促期间顺延1-2天。"),
    ("物流停滞处理", "物流超过72小时无更新可联系客服核实，必要时我们会补发或退款，无需您承担损失。"),
    ("配送范围说明", "全国大部分地区可达，偏远地区（新疆、西藏等）需加收运费且时效延长3-5天。"),
    ("修改绑定手机", "在『账号设置-安全中心』通过原手机验证码或人脸验证后可更换绑定手机号。"),
    ("找回登录密码", "登录页点击『忘记密码』，通过绑定手机或邮箱接收验证码后即可重置登录密码。"),
    ("注销账号说明", "账号注销不可恢复，需先结清订单与售后、解绑第三方，注销后积分与优惠券清零。"),
    ("账号被盗处理", "若账号异常登录请立即改密并冻结账号，联系客服核实身份后协助找回与排查。"),
    ("支持支付方式", "支持微信、支付宝、银联以及部分机型的花呗分期，暂不支持货到付款。"),
    ("重复扣款处理", "支付时网络异常可能重复扣款，多扣部分会在1-3工作日自动原路退回，未退请联系客服。"),
    ("发票开具说明", "支持开具电子普通发票与增值税专用发票，下单后30天内在订单页申请并填写抬头。"),
    ("积分使用规则", "积分可在结算时抵现，100积分抵1元，单笔最多抵扣订单金额的50%，积分有效期一年。"),
    ("优惠券使用规则", "优惠券需满足使用门槛，不可叠加不可兑现，逾期作废，退货后券一般不退回。"),
]

# (query, expected_title) — query targets ONE item whose cluster-mates are close distractors.
QUERIES: list[tuple[str, str]] = [
    ("退款一般多久能到账", "退款到账时效"),
    ("退款能退到别的银行卡吗", "退款方式说明"),
    ("还没发货可以退款吗", "退款申请条件"),
    ("在哪里看退款到哪一步了", "退款进度查询"),
    ("退款一直失败怎么办", "退款失败处理"),
    ("七天无理由退货运费谁承担", "退货邮费谁出"),
    ("怎么寄回退货的商品", "退货操作步骤"),
    ("买大了想换个尺码可以吗", "换货政策说明"),
    ("预售的东西什么时候发货", "发货时效说明"),
    ("快递好几天不动了怎么办", "物流停滞处理"),
    ("新疆能送到吗要加钱吗", "配送范围说明"),
    ("怎么换绑定的手机号码", "修改绑定手机"),
    ("密码忘了怎么找回来", "找回登录密码"),
    ("我想把账号注销掉", "注销账号说明"),
    ("账号被别人登录了怎么办", "账号被盗处理"),
    ("你们都能用什么付款", "支持支付方式"),
    ("付款扣了两次钱", "重复扣款处理"),
    ("能开专票吗", "发票开具说明"),
    ("积分怎么用能抵多少钱", "积分使用规则"),
    ("优惠券能不能一起用", "优惠券使用规则"),
]

# Off-topic queries the corpus can't answer — retrieval SHOULD return []. Without
# them every floor-type sweep (vector_min_sim, trgm_threshold) reads "lower = more
# recall = better", which is exactly the wrong conclusion.
NEGATIVE_QUERIES: list[str] = [
    "今天天气怎么样",
    "帮我写一首关于春天的诗",
    "世界杯决赛是哪天踢",
    "附近有什么好吃的火锅店",
    "如何自学钢琴入门",
]

# ---- Long multi-section docs (chunk_size sensitivity) + section-targeting queries ----
_SECTIONS = [
    ("账户安全", "建议开启两步验证，定期更换高强度密码，不要在公共设备保存登录状态，发现异常立即冻结。"),
    ("订单修改", "未发货订单可在30分钟内修改收货地址与规格，超时或已发货请走售后或拒收后重拍。"),
    ("发票与对公", "企业客户可申请对公转账与增值税专用发票，需提供开票资质，账期与额度由商务评估。"),
    ("会员体系", "消费累计成长值升级会员等级，等级对应不同折扣、专属客服与免邮次数，等级每年复核。"),
    ("隐私与数据", "我们仅收集必要信息用于履约，依据隐私政策保存，可申请导出或删除个人数据。"),
]


def _long_doc(idx: int) -> tuple[str, str]:
    body = "\n\n".join(f"【{name}】{txt * 4}" for name, txt in _SECTIONS)
    return (f"服务条款与使用指南 第{idx}册", f"本手册涵盖账户、订单、发票、会员、隐私等条款。\n\n{body}")


# Shared, non-distinguishing filler appended to every cluster item. It makes each item
# multi-chunk and, at large chunk_size, dilutes the one distinguishing sentence with text
# common to the whole cluster — the exact condition under which smaller chunks should win.
_PAD = (
    "为保障您的权益，请在操作前仔细阅读相关条款与常见问题，确保理解适用范围与限制条件。"
    "如对流程或结果有疑问，可在工作时间联系在线客服或拨打服务热线，我们会安排专人跟进处理。"
    "平台会持续优化服务体验，具体规则可能随活动或政策调整，最终以下单页面与订单详情展示为准。"
    "感谢您的理解与支持，祝您购物愉快。"
)


def build_corpus() -> list[tuple[str, str]]:
    # distinguishing sentence first, then shared filler → multi-chunk, cluster-converging.
    items = [(t, f"{c}\n\n{_PAD * 2}") for t, c in CLUSTERS]
    items += [_long_doc(i) for i in range(1, 5)]  # 4 long docs (each ~10+ chunks)
    items += [(f"产品介绍 {i}", f"这是第{i}款产品的卖点、规格参数与适用场景的简要介绍。" * 2) for i in range(1, 26)]
    return items


# ---------------------------------------------------------------- helpers
async def _chunk_and_store(db: AsyncSession, item: KnowledgeItem, emb, chunk_size: int, overlap: int):
    from app.agent.context import estimate_tokens
    from app.llm.factory import get_embedding_client
    from app.rag.chunking import chunk_text
    from app.rag.segment import segment

    # Ingest rule (mirror of services.knowledge.reembed_item): chunk the bare content so
    # stored chunks stay citation-accurate; the title is prefixed into the embedding
    # input AND content_seg so every chunk — not just chunk 0 — keeps its topic anchor
    # on both retrieval paths.
    pieces = chunk_text(item.content, chunk_size, overlap)
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))
    inputs = [f"{item.title}\n{p}" if item.title else p for p in pieces]
    vectors = await get_embedding_client(emb).embed(inputs) if pieces else []
    for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=False)):
        db.add(KnowledgeChunk(
            tenant_id=item.tenant_id, item_id=item.id, chunk_index=i, content=piece,
            content_seg=segment(f"{item.title}\n{piece}" if item.title else piece),
            embedding=vec, embedding_model=emb.model,
            embedding_dim=emb.dim, status="ready", token_count=estimate_tokens(piece),
        ))
    await db.flush()
    return len(pieces)


async def _metrics(db, cfg, top_k: int = TOP_K) -> tuple[float, float, float]:
    """(recall@top_k, MRR, correct-rejection-rate) under `cfg`."""
    hits, rr = 0, 0.0
    for q, exp in QUERIES:
        res = await hybrid_search(db, cfg, q, top_k=top_k)
        rank = next((i + 1 for i, r in enumerate(res) if exp in (r.title or "")), 0)
        if rank:
            hits += 1
            rr += 1.0 / rank
    rejected = 0
    for q in NEGATIVE_QUERIES:
        if not await hybrid_search(db, cfg, q, top_k=top_k):
            rejected += 1
    return hits / len(QUERIES), rr / len(QUERIES), rejected / len(NEGATIVE_QUERIES)


async def _set_ef_construction(db, ef: int) -> float:
    t0 = time.monotonic()
    await db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
    await db.execute(text(
        f"CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
        f"USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = {ef})"
    ))
    await db.commit()
    return time.monotonic() - t0


async def _sweep_params(
    db, cfg, header: str, rows: list[tuple[str, dict]], top_k: int = TOP_K
) -> None:
    """One table per sweep; each row is (label, retrieval-key overrides). Overrides are
    in-memory only (never committed) and restored afterwards. Cheap: no re-embedding."""
    orig = dict(cfg.retrieval or {})
    print(f"\n--- {header} (top_k={top_k}) ---")
    print(f"{'value':>20} | {'recall@' + str(top_k):>9} | {'MRR':>6} | {'reject':>7}")
    print("-" * 54)
    try:
        for label, overrides in rows:
            cfg.retrieval = {**orig, **overrides}
            rec, mrr, rej = await _metrics(db, cfg, top_k=top_k)
            print(f"{label:>20} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")
    finally:
        cfg.retrieval = orig


async def _sweep_rrf_k(db, cfg) -> None:
    """RRF damping constant K. Not a retrieval config key — patch the module constant.
    Swept at top_k=5 so the candidate pool (max(5*8, 20) = 40) matches production; the
    small K end sharpens head ranks but also amplifies a noisy keyword top-1, hence the
    reject column matters here."""
    import app.rag.retrieval as retrieval_mod

    if not hasattr(retrieval_mod, "_RRF_K"):
        print("\n--- RRF K sweep: skipped — retrieval._RRF_K 已不存在（被重构为参数？），改用其新机制扫描。")
        return
    orig_k = retrieval_mod._RRF_K
    print(f"\n--- RRF K sweep (top_k=5, pool≈40, 当前默认 K={orig_k}) ---")
    print(f"{'K':>6} | {'recall@5':>9} | {'MRR':>6} | {'reject':>7}")
    print("-" * 40)
    try:
        for k in (10, 20, 40, 60):
            retrieval_mod._RRF_K = k
            rec, mrr, rej = await _metrics(db, cfg, top_k=5)
            print(f"{k:>6} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")
    finally:
        retrieval_mod._RRF_K = orig_k


async def _single_path_report(db, cfg) -> None:
    """Vector-only vs keyword-only recall@K — the fused number alone can't say which
    side of the fusion weights is carrying which queries."""
    from app.rag.retrieval import _embed_query, _keyword_search, _vector_search
    from app.services.ai_config import to_embedding_settings

    params = merged_retrieval(cfg)
    emb_cfg = to_embedding_settings(cfg)
    try:
        v_hits = k_hits = 0
        for q, exp in QUERIES:
            qvec = await _embed_query(emb_cfg, q)
            if qvec is not None:
                vrows, _ = await _vector_search(
                    db, emb_cfg, qvec, TOP_K, float(params["vector_min_sim"])
                )
                v_hits += any(exp in (r[2] or "") for r in vrows)
            krows = await _keyword_search(db, q, TOP_K, float(params["trgm_threshold"]))
            k_hits += any(exp in (r[2] or "") for r in krows)
        n = len(QUERIES)
        print(f"\n--- single-path recall@{TOP_K} ---")
        print(f"vector-only : {v_hits}/{n} = {v_hits / n:.0%}")
        print(f"keyword-only: {k_hits}/{n} = {k_hits / n:.0%}")
    except Exception as exc:  # noqa: BLE001 — private helpers; skip if their shape changed
        print(f"\nsingle-path report skipped（retrieval 内部接口变了？）: {exc}")


# ---------------------------------------------------------------- main
async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        from app.services.ai_config import to_embedding_settings
        cfg = await get_active_ai_config(db)
        emb = to_embedding_settings(cfg)
        overlap = 50  # fixed small overlap so it stays valid across the small chunk_sizes swept

        # Residue from a crashed previous run would skew every sweep — clear it first.
        left = (
            await db.execute(delete(KnowledgeItem).where(KnowledgeItem.category == TAG))
        ).rowcount or 0
        await db.commit()
        if left:
            print(f"removed {left} leftover {TAG} item(s) from a previous run")

        ids: list = []
        index_touched = False
        try:
            # seed corpus (unembedded shells first)
            for title, content in build_corpus():
                it = KnowledgeItem(title=title, content=content, category=TAG, status="published")
                db.add(it)
                await db.flush()
                ids.append(it.id)
            await db.commit()
            print(f"seeded {len(ids)} synthetic items ({len(QUERIES)} labelled + {len(NEGATIVE_QUERIES)} negative queries)\n")

            # ---- sweep chunk_size (re-embed corpus each time) ----
            print(f"--- chunk_size sweep (overlap={overlap}, top_k={TOP_K}) ---")
            print(f"{'chunk_size':>11} | {'chunks':>6} | {'recall@' + str(TOP_K):>9} | {'MRR':>6} | {'reject':>7}")
            print("-" * 54)
            for cs in (120, 200, 350, 600):
                total = 0
                for iid in ids:
                    it = await db.get(KnowledgeItem, iid)
                    total += await _chunk_and_store(db, it, emb, cs, overlap)
                await db.commit()
                cfg.retrieval = {**(cfg.retrieval or {}), "chunk_size": cs}
                rec, mrr, rej = await _metrics(db, cfg)
                print(f"{cs:>11} | {total:>6} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")

            # restore chunk_size to 600 for the remaining sweeps
            for iid in ids:
                it = await db.get(KnowledgeItem, iid)
                await _chunk_and_store(db, it, emb, 600, overlap)
            await db.commit()
            cfg.retrieval = {**(cfg.retrieval or {}), "chunk_size": 600}

            # ---- cheap retrieval-param sweeps (no re-embedding, chunk_size=600) ----
            await _single_path_report(db, cfg)
            await _sweep_params(db, cfg, "fusion weights sweep", [
                (f"v{vw:.1f}/k{kw:.1f}", {"vector_weight": vw, "keyword_weight": kw})
                for vw, kw in ((0.7, 0.3), (0.6, 0.4), (0.5, 0.5))
            ])
            await _sweep_params(db, cfg, "trgm_threshold sweep", [
                (f"{t:.2f}", {"trgm_threshold": t}) for t in (0.2, 0.3, 0.4, 0.5, 0.6)
            ])
            await _sweep_params(db, cfg, "vector_min_sim sweep", [
                (f"{v:.2f}", {"vector_min_sim": v}) for v in (0.35, 0.45, 0.50, 0.55, 0.60)
            ])
            # Pool = max(top_k*mult, 20): at top_k=3 the 20-floor clamps the low end,
            # so sweep at production top_k=5 (pool 20/40/60).
            await _sweep_params(db, cfg, "candidate_multiplier sweep", [
                (str(m), {"candidate_multiplier": m}) for m in (4, 8, 12)
            ], top_k=5)
            await _sweep_rrf_k(db, cfg)

            # ---- sweep HNSW ef_construction (index rebuild only) ----
            print("\n--- HNSW ef_construction sweep (chunk_size=600) ---")
            print(f"{'ef_construction':>15} | {'build_s':>7} | {'recall@' + str(TOP_K):>9} | {'MRR':>6} | {'reject':>7}")
            print("-" * 56)
            index_touched = True
            for ef in (64, 200, 400):
                build_s = await _set_ef_construction(db, ef)
                rec, mrr, rej = await _metrics(db, cfg)
                print(f"{ef:>15} | {build_s:>7.2f} | {rec:>9.0%} | {mrr:>6.3f} | {rej:>7.0%}")
        finally:
            # rollback first: cleanup must run even when a failure aborted the tx.
            await db.rollback()
            try:
                if index_touched:
                    await _set_ef_construction(db, _DEFAULT_EF)
            except Exception as exc:  # noqa: BLE001
                log.warning("ef_restore_failed", error=str(exc))
                print(f"!! 未能恢复 HNSW 索引（ef_construction={_DEFAULT_EF}）：{exc} — 请手工重建，否则向量检索退化。")
            removed = (
                await db.execute(delete(KnowledgeItem).where(KnowledgeItem.category == TAG))
            ).rowcount or 0
            await db.commit()
            print(f"\ncleaned up {removed} synthetic item(s); index restored to ef_construction={_DEFAULT_EF}.")


if __name__ == "__main__":
    asyncio.run(main())
