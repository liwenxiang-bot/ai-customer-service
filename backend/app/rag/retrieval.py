"""Hybrid retrieval: vector (semantic) + keyword (exact) → fusion → optional rerank.

This is the core of answer quality. Pure vector search misses exact terms (product
models, error codes); pure keyword misses paraphrases. We run both and fuse with
weighted Reciprocal Rank Fusion (scale-free, robust), then optionally rerank.

Every stage degrades gracefully: if embeddings are unavailable we fall back to
keyword-only; if the whole thing fails the caller falls back to a pure-LLM answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import retrieval_calls
from app.llm.factory import get_embedding_client, get_rerank_client
from app.models.config import AIConfig
from app.services.ai_config import (
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
    content: str
    score: float


async def _vector_search(
    db: AsyncSession, cfg: AIConfig, query: str, limit: int
) -> list[tuple[str, str, str, str]]:
    """Return [(item_id, chunk_id, title, content)] by cosine distance, or [] on failure."""
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return []
    try:
        client = get_embedding_client(emb_cfg)
        qvec = await client.embed_one(query)
        if not qvec:
            return []
        # Cast the bound array literal to the column's vector type for the <=> operator.
        sql = text(
            """
            SELECT c.item_id::text, c.id::text, i.title, c.content
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
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except Exception as exc:  # noqa: BLE001 — degrade, never break the turn
        log.warning("vector_search_degraded", error=str(exc))
        return []


async def _keyword_search(
    db: AsyncSession, query: str, limit: int
) -> list[tuple[str, str, str, str]]:
    """Full-text (tsvector) + trigram exact-term matching."""
    try:
        sql = text(
            """
            SELECT c.item_id::text, c.id::text, i.title, c.content,
                   ts_rank_cd(c.tsv, websearch_to_tsquery('simple', :q)) AS ts_score,
                   similarity(c.content, :q) AS trgm_score
            FROM knowledge_chunks c
            JOIN knowledge_items i ON i.id = c.item_id
            WHERE i.status = 'published'
              AND (c.tsv @@ websearch_to_tsquery('simple', :q)
                   OR similarity(c.content, :q) > 0.05)
            ORDER BY (ts_rank_cd(c.tsv, websearch_to_tsquery('simple', :q)) * 2
                      + similarity(c.content, :q)) DESC
            LIMIT :limit
            """
        )
        rows = (await db.execute(sql, {"q": query, "limit": limit})).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("keyword_search_degraded", error=str(exc))
        return []


def _fuse(
    vector_hits: list[tuple], keyword_hits: list[tuple], vw: float, kw: float
) -> list[tuple[str, str, str, str, float]]:
    """Weighted Reciprocal Rank Fusion over the two ranked lists (keyed by chunk_id)."""
    scores: dict[str, float] = {}
    payload: dict[str, tuple] = {}
    for rank, hit in enumerate(vector_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + vw / (_RRF_K + rank)
        payload[cid] = hit
    for rank, hit in enumerate(keyword_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + kw / (_RRF_K + rank)
        payload.setdefault(cid, hit)
    fused = [(*payload[cid], sc) for cid, sc in scores.items()]
    fused.sort(key=lambda x: x[4], reverse=True)
    return fused


async def hybrid_search(
    db: AsyncSession, cfg: AIConfig, query: str, top_k: int | None = None
) -> list[RetrievalResult]:
    params = cfg.retrieval or {}
    top_k = top_k or int(params.get("top_k", 5))
    vw = float(params.get("vector_weight", 0.6))
    kw = float(params.get("keyword_weight", 0.4))
    candidate_n = max(top_k * 4, 12)

    vector_hits = await _vector_search(db, cfg, query, candidate_n)
    keyword_hits = await _keyword_search(db, query, candidate_n)
    if not vector_hits and not keyword_hits:
        retrieval_calls.labels("miss").inc()
        return []
    retrieval_calls.labels("hit").inc()

    fused = _fuse(vector_hits, keyword_hits, vw, kw)

    # Optional rerank over the fused candidates.
    rer_cfg = to_rerank_settings(cfg)
    if rer_cfg and rer_cfg.api_key and len(fused) > 1:
        top_n = int(params.get("rerank_top_n", top_k))
        docs = [f"{f[2]}\n{f[3]}" for f in fused[:candidate_n]]
        order = await get_rerank_client(rer_cfg).rerank(query, docs, top_n)
        # Reorder fused candidates by rerank result, replacing the fusion score.
        fused = [(fused[i][0], fused[i][1], fused[i][2], fused[i][3], sc) for i, sc in order]

    results = [
        RetrievalResult(item_id=f[0], chunk_id=f[1], title=f[2], content=f[3], score=round(f[4], 4))
        for f in fused[:top_k]
    ]
    return results
