"""Parameter-sweep eval for RAG retrieval.

Unlike scripts/rag_eval (a tiny smoke set), this synthesizes a *hard* corpus —
semantically-close clusters (退款时效 vs 退款方式 …) + long multi-section docs + noise —
so that chunk_size and HNSW ef_construction differences actually surface. It sweeps each
parameter, prints recall@k / MRR, and cleans up after itself.

Run:  python -m scripts.rag_param_eval
Heavy on the embedding API (re-embeds the corpus once per chunk_size). Marked items use
category='__param_eval__' and are deleted at the end.
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
from app.services.ai_config import get_active_ai_config

log = get_logger("rag_param_eval")
TAG = "__param_eval__"
TOP_K = 3

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

    txt = f"{item.title}\n{item.content}" if item.title else item.content
    pieces = chunk_text(txt, chunk_size, overlap)
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))
    vectors = await get_embedding_client(emb).embed(pieces) if pieces else []
    for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=False)):
        db.add(KnowledgeChunk(
            tenant_id=item.tenant_id, item_id=item.id, chunk_index=i, content=piece,
            content_seg=segment(piece), embedding=vec, embedding_model=emb.model,
            embedding_dim=emb.dim, status="ready", token_count=estimate_tokens(piece),
        ))
    await db.flush()
    return len(pieces)


async def _metrics(db, cfg) -> tuple[float, float]:
    hits, rr = 0, 0.0
    for q, exp in QUERIES:
        res = await hybrid_search(db, cfg, q, top_k=TOP_K)
        rank = next((i + 1 for i, r in enumerate(res) if exp in (r.title or "")), 0)
        if rank:
            hits += 1
            rr += 1.0 / rank
    return hits / len(QUERIES), rr / len(QUERIES)


async def _set_ef_construction(db, ef: int) -> float:
    t0 = time.monotonic()
    await db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
    await db.execute(text(
        f"CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
        f"USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = {ef})"
    ))
    await db.commit()
    return time.monotonic() - t0


# ---------------------------------------------------------------- main
async def main() -> None:
    configure_logging()
    async with session_scope() as db:
        from app.services.ai_config import to_embedding_settings
        cfg = await get_active_ai_config(db)
        emb = to_embedding_settings(cfg)
        overlap = 50  # fixed small overlap so it stays valid across the small chunk_sizes swept

        # seed corpus (unembedded shells first)
        ids = []
        for title, content in build_corpus():
            it = KnowledgeItem(title=title, content=content, category=TAG, status="published")
            db.add(it)
            await db.flush()
            ids.append(it.id)
        await db.commit()
        print(f"seeded {len(ids)} synthetic items ({len(QUERIES)} labelled queries)\n")

        # ---- sweep chunk_size (re-embed corpus each time) ----
        print(f"--- chunk_size sweep (overlap={overlap}, top_k={TOP_K}) ---")
        print(f"{'chunk_size':>11} | {'chunks':>6} | {'recall@' + str(TOP_K):>9} | {'MRR':>6}")
        print("-" * 44)
        for cs in (120, 200, 350, 600):
            total = 0
            for iid in ids:
                it = await db.get(KnowledgeItem, iid)
                total += await _chunk_and_store(db, it, emb, cs, overlap)
            await db.commit()
            cfg.retrieval = {**(cfg.retrieval or {}), "chunk_size": cs}
            rec, mrr = await _metrics(db, cfg)
            print(f"{cs:>11} | {total:>6} | {rec:>9.0%} | {mrr:>6.3f}")

        # restore chunk_size to 600 for the ef_construction sweep
        for iid in ids:
            it = await db.get(KnowledgeItem, iid)
            await _chunk_and_store(db, it, emb, 600, overlap)
        await db.commit()
        cfg.retrieval = {**(cfg.retrieval or {}), "chunk_size": 600}

        # ---- sweep HNSW ef_construction (index rebuild only) ----
        print("\n--- HNSW ef_construction sweep (chunk_size=600) ---")
        print(f"{'ef_construction':>15} | {'build_s':>7} | {'recall@'+str(TOP_K):>9} | {'MRR':>6}")
        print("-" * 46)
        for ef in (64, 200, 400):
            build_s = await _set_ef_construction(db, ef)
            rec, mrr = await _metrics(db, cfg)
            print(f"{ef:>15} | {build_s:>7.2f} | {rec:>9.0%} | {mrr:>6.3f}")

        # ---- cleanup: drop synthetic items + restore default index ----
        await _set_ef_construction(db, 64)  # pgvector default
        await db.execute(delete(KnowledgeItem).where(KnowledgeItem.id.in_(ids)))
        await db.commit()
        print("\ncleaned up synthetic corpus; index restored to default ef_construction.")


if __name__ == "__main__":
    asyncio.run(main())
