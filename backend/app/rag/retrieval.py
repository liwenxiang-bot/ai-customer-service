"""Hybrid retrieval: vector (semantic) + keyword (exact) → fusion → optional rerank.

This is the core of answer quality. Pure vector search misses exact terms (product
models, error codes); pure keyword misses paraphrases. We run both and fuse with
weighted Reciprocal Rank Fusion (scale-free, robust), then optionally rerank.

Relevance gating (so we don't "always hit" the nearest-but-irrelevant chunk):
  - vector path drops chunks below `vector_min_sim` (cosine similarity) BEFORE fusion;
  - when rerank is on, results below `min_score` (rerank score) are dropped after it;
  - the keyword query is jieba-segmented so Chinese full-text matching actually works.
A query with nothing relevant now returns [] → the tool tells the model to escalate
instead of answering from weak context.

Chinese keyword search: `tsv` indexes a jieba-segmented copy of the content (see
rag.segment); we segment the query the same way so 'simple' tsquery can match.

Every stage degrades gracefully: if embeddings are unavailable we fall back to
keyword-only; if the whole thing fails the caller falls back to a pure-LLM answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import retrieval_calls
from app.llm.factory import get_embedding_client, get_rerank_client
from app.models.config import AIConfig
from app.rag.segment import segment
from app.services.ai_config import (
    merged_retrieval,
    to_embedding_settings,
    to_rerank_settings,
)

log = get_logger("rag.retrieval")

_RRF_K = 60  # RRF damping constant


@dataclass
class RetrievalResult:
    item_id: str
    chunk_id: str
    title: str
    content: str  # the matched chunk — citation-accurate
    score: float
    context: str = ""  # matched chunk expanded with neighbours (fed to the LLM)


async def _vector_search(
    db: AsyncSession, cfg: AIConfig, query: str, limit: int, min_sim: float
) -> list[tuple[str, str, str, str, float]]:
    """[(item_id, chunk_id, title, content, similarity)] by cosine, gated by min_sim.

    Returns [] on any failure (degrade to keyword-only)."""
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return []
    try:
        client = get_embedding_client(emb_cfg)
        qvec = await client.embed_one(query)
        if not qvec:
            return []
        # ORDER BY <=> LIMIT uses the HNSW index; we return similarity and apply the
        # floor in Python so the index scan stays fast.
        sql = text(
            """
            SELECT c.item_id::text, c.id::text, i.title, c.content,
                   1 - (c.embedding <=> CAST(:qvec AS vector)) AS sim
            FROM knowledge_chunks c
            JOIN knowledge_items i ON i.id = c.item_id
            WHERE c.embedding IS NOT NULL
              AND c.status = 'ready'
              AND i.status = 'published'
              AND c.embedding_dim = :dim
            ORDER BY c.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
            """
        )
        qvec_literal = "[" + ",".join(str(x) for x in qvec) + "]"
        rows = (
            await db.execute(sql, {"qvec": qvec_literal, "limit": limit, "dim": emb_cfg.dim})
        ).all()
        return [
            (r[0], r[1], r[2], r[3], float(r[4]))
            for r in rows
            if r[4] is not None and float(r[4]) >= min_sim
        ]
    except Exception as exc:  # noqa: BLE001 — degrade, never break the turn
        log.warning("vector_search_degraded", error=str(exc))
        return []


async def _keyword_search(
    db: AsyncSession, query: str, limit: int, trgm_threshold: float
) -> list[tuple[str, str, str, str]]:
    """Full-text (jieba-segmented tsvector) + trigram exact-term matching."""
    try:
        q_seg = segment(query) or query
        sql = text(
            """
            SELECT c.item_id::text, c.id::text, i.title, c.content
            FROM knowledge_chunks c
            JOIN knowledge_items i ON i.id = c.item_id
            WHERE i.status = 'published'
              AND (c.tsv @@ websearch_to_tsquery('simple', :q_seg)
                   OR similarity(c.content, :q) > :trgm)
            ORDER BY (ts_rank_cd(c.tsv, websearch_to_tsquery('simple', :q_seg)) * 2
                      + similarity(c.content, :q)) DESC
            LIMIT :limit
            """
        )
        rows = (
            await db.execute(
                sql, {"q_seg": q_seg, "q": query, "trgm": trgm_threshold, "limit": limit}
            )
        ).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("keyword_search_degraded", error=str(exc))
        return []


def _fuse(
    vector_hits: list[tuple], keyword_hits: list[tuple], vw: float, kw: float
) -> list[tuple[str, str, str, str, float, float | None]]:
    """Weighted RRF over the two ranked lists (keyed by chunk_id).

    Output rows are (item_id, chunk_id, title, content, rrf_score, sim) where `sim` is
    the vector cosine similarity if the chunk was a vector hit, else None. Accepts both
    4-tuples and 5-tuples (with similarity) as input."""
    scores: dict[str, float] = {}
    payload: dict[str, tuple] = {}
    sims: dict[str, float] = {}
    for rank, hit in enumerate(vector_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + vw / (_RRF_K + rank)
        payload[cid] = hit[:4]
        if len(hit) > 4 and hit[4] is not None:
            sims[cid] = float(hit[4])
    for rank, hit in enumerate(keyword_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + kw / (_RRF_K + rank)
        payload.setdefault(cid, hit[:4])
    fused = [(*payload[cid], sc, sims.get(cid)) for cid, sc in scores.items()]
    fused.sort(key=lambda x: x[4], reverse=True)
    return fused


async def _expand_context(db: AsyncSession, chunk_ids: list[str]) -> dict[str, str]:
    """Small-to-big: {chunk_id: content merged with its immediate neighbours}.

    Retrieval matches small chunks (precise), but a 600-char slice can sever the answer.
    We hand the LLM each matched chunk plus its prev/next sibling in the same item."""
    if not chunk_ids:
        return {}
    try:
        sql = text(
            """
            SELECT center.id::text AS cid,
                   string_agg(c.content, chr(10) ORDER BY c.chunk_index) AS merged
            FROM knowledge_chunks center
            JOIN knowledge_chunks c
              ON c.item_id = center.item_id
             AND c.chunk_index BETWEEN center.chunk_index - 1 AND center.chunk_index + 1
            WHERE center.id IN :ids
            GROUP BY center.id
            """
        ).bindparams(bindparam("ids", expanding=True))
        rows = (
            await db.execute(sql, {"ids": [uuid.UUID(c) for c in chunk_ids]})
        ).all()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception as exc:  # noqa: BLE001 — expansion is a bonus, never break retrieval
        log.warning("context_expand_degraded", error=str(exc))
        return {}


async def hybrid_search(
    db: AsyncSession, cfg: AIConfig, query: str, top_k: int | None = None
) -> list[RetrievalResult]:
    params = merged_retrieval(cfg)  # defaults + DB (DB wins; nulls fall back to defaults)
    top_k = top_k or int(params["top_k"])
    vw = float(params["vector_weight"])
    kw = float(params["keyword_weight"])
    vector_min_sim = float(params["vector_min_sim"])
    min_score = float(params["min_score"])
    trgm_threshold = float(params["trgm_threshold"])
    candidate_multiplier = int(params["candidate_multiplier"])
    candidate_n = max(top_k * candidate_multiplier, 20)

    vector_hits = await _vector_search(db, cfg, query, candidate_n, vector_min_sim)
    keyword_hits = await _keyword_search(db, query, candidate_n, trgm_threshold)
    if not vector_hits and not keyword_hits:
        retrieval_calls.labels("miss").inc()
        return []

    fused = _fuse(vector_hits, keyword_hits, vw, kw)

    # Optional rerank over the fused candidates (on the matched chunk text).
    rer_cfg = to_rerank_settings(cfg)
    reranked = False
    if rer_cfg and rer_cfg.api_key and len(fused) > 1:
        top_n = max(int(params["rerank_top_n"]), top_k)
        pool = fused[:candidate_n]
        docs = [f"{f[2]}\n{f[3]}" for f in pool]
        order = await get_rerank_client(rer_cfg).rerank(query, docs, top_n)
        # All-zero scores ⇒ the client degraded (failure); keep the fusion order.
        if order and any(sc > 0 for _, sc in order):
            reranked = True
            fused = [(*pool[i][:4], sc, sc) for i, sc in order]

    # Relevance floor: rerank scores are gated by min_score; without rerank the vector
    # path is already gated by vector_min_sim and keyword-only hits cleared the trgm/tsv
    # gate, so we keep them.
    if reranked and min_score > 0:
        fused = [f for f in fused if f[4] >= min_score]

    results_src = fused[:top_k]
    if not results_src:
        retrieval_calls.labels("filtered").inc()  # recalled, but nothing cleared the floor
        return []
    retrieval_calls.labels("hit").inc()

    expanded = (
        await _expand_context(db, [f[1] for f in results_src])
        if params["expand_context"]
        else {}
    )

    return [
        RetrievalResult(
            item_id=f[0],
            chunk_id=f[1],
            title=f[2],
            content=f[3],
            score=round(float(f[4]), 4),
            context=expanded.get(f[1], f[3]),
        )
        for f in results_src
    ]
