"""Semantic cache: high-frequency questions return a stored answer without an LLM call.

Off by default. Requires embeddings — degrades to a no-op when none are configured.
Saves cost/latency on repeated FAQs (requirements §6 推荐).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.llm.factory import get_embedding_client
from app.models.config import AIConfig
from app.models.usage import SemanticCacheEntry
from app.services.ai_config import to_embedding_settings

log = get_logger("semantic_cache")


async def lookup(db: AsyncSession, cfg: AIConfig, query: str) -> dict | None:
    if not cfg.semantic_cache_enabled:
        return None
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return None
    try:
        qvec = await get_embedding_client(emb_cfg).embed_one(query)
        if not qvec:
            return None
        qlit = "[" + ",".join(str(x) for x in qvec) + "]"
        # cosine distance = 1 - similarity; threshold is on similarity.
        max_dist = 1.0 - settings.semantic_cache_threshold
        row = (
            await db.execute(
                text(
                    """
                    SELECT id::text, answer, citations,
                           (embedding <=> CAST(:q AS vector)) AS dist
                    FROM semantic_cache
                    WHERE embedding IS NOT NULL AND embedding_model = :model
                    ORDER BY embedding <=> CAST(:q AS vector)
                    LIMIT 1
                    """
                ),
                {"q": qlit, "model": emb_cfg.model},
            )
        ).first()
        if row and row.dist is not None and row.dist <= max_dist:
            await db.execute(
                text("UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE id = :id"),
                {"id": row.id},
            )
            log.info("semantic_cache_hit", dist=round(row.dist, 4))
            return {"answer": row.answer, "citations": row.citations or []}
    except Exception as exc:  # noqa: BLE001
        log.warning("semantic_cache_lookup_failed", error=str(exc))
    return None


async def store(db: AsyncSession, cfg: AIConfig, query: str, answer: str, citations: list) -> None:
    if not cfg.semantic_cache_enabled or not answer:
        return
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return
    try:
        qvec = await get_embedding_client(emb_cfg).embed_one(query)
        if not qvec:
            return
        db.add(
            SemanticCacheEntry(
                query_text=query,
                embedding=qvec,
                answer=answer,
                citations=citations or [],
                embedding_model=emb_cfg.model,
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("semantic_cache_store_failed", error=str(exc))
