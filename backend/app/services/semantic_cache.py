"""Semantic cache: high-frequency questions return a stored answer without an LLM call.

Off by default. Requires embeddings — degrades to a no-op when none are configured.
Saves cost/latency on repeated FAQs (requirements §6 推荐).

Cached answers can go stale or plain wrong, so three guards bound the blast radius:
  * context-dependent queries ("它多少钱") are never matched nor stored — the referent
    lives in earlier turns, so an embedding match across sessions returns someone
    else's answer (see _is_context_dependent);
  * rows older than settings.semantic_cache_ttl_hours are ignored on lookup — the
    backstop for knowledge edits that slipped past invalidate_all();
  * invalidate_all() flushes the whole cache (per tenant, via RLS) when the knowledge
    base changes, and store() prunes the oldest rows past a hard per-tenant cap.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.llm.factory import get_embedding_client
from app.models.config import AIConfig
from app.models.usage import SemanticCacheEntry
from app.services.ai_config import to_embedding_settings

log = get_logger("semantic_cache")

# Per-tenant row cap (RLS scopes both the count and the prune). Keeps the ANN search
# space — and the odds of a 0.95 near-miss — bounded; the table has no created_at
# index, but the cap itself keeps the prune's sort cheap.
_MAX_ROWS = 10_000

# Anaphora markers: the query's subject was established in earlier turns, so the text
# alone is not a valid cache key. Substring matching over-triggers on words like
# 其他 (contains 他), which is fine — a false positive only costs a cache miss,
# a false negative returns another conversation's answer.
_ANAPHORA = (
    "它", "他", "她",
    "这个", "那个", "这款", "那款", "这些", "那些", "这种", "那种",
    "上面", "上述", "前面", "刚才", "刚刚", "以上",
)

# Error codes / SKUs / model numbers ("E1001", "SKU-12"): short but self-contained.
_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")


def _is_context_dependent(query: str) -> bool:
    """True when the query cannot stand alone as a cache key (F26).

    Two cheap signals: anaphora words (referent lives in the conversation, not the
    text), and very short queries without a code-like token ("多少钱" means a
    different product in every session). Applied on BOTH lookup and store: store-side
    filtering alone cannot protect against dirty rows already in the table.
    """
    q = query.strip()
    if any(w in q for w in _ANAPHORA):
        return True
    return len(q) < 6 and not _CODE_TOKEN.search(q)


async def lookup(db: AsyncSession, cfg: AIConfig, query: str) -> dict | None:
    if not cfg.semantic_cache_enabled:
        return None
    if _is_context_dependent(query):
        return None
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return None
    try:
        # text_type must match store(): both sides embed user queries, and the 0.95
        # threshold only holds for vectors from the same embedding space.
        qvec = await get_embedding_client(emb_cfg).embed_one(query, text_type="query")
        if not qvec:
            return None
        qlit = "[" + ",".join(str(x) for x in qvec) + "]"
        # cosine distance = 1 - similarity; threshold is on similarity.
        max_dist = 1.0 - settings.semantic_cache_threshold
        params: dict = {"q": qlit, "model": emb_cfg.model}
        ttl_sql = ""
        if settings.semantic_cache_ttl_hours > 0:
            ttl_sql = "AND created_at > now() - make_interval(hours => :ttl)"
            params["ttl"] = settings.semantic_cache_ttl_hours
        row = (
            await db.execute(
                text(
                    f"""
                    SELECT id::text, answer, citations,
                           (embedding <=> CAST(:q AS vector)) AS dist
                    FROM semantic_cache
                    WHERE embedding IS NOT NULL AND embedding_model = :model {ttl_sql}
                    ORDER BY embedding <=> CAST(:q AS vector)
                    LIMIT 1
                    """
                ),
                params,
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
    if _is_context_dependent(query):
        return
    emb_cfg = to_embedding_settings(cfg)
    if not emb_cfg.api_key:
        return
    try:
        qvec = await get_embedding_client(emb_cfg).embed_one(query, text_type="query")
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
        await _prune_over_cap(db)
    except Exception as exc:  # noqa: BLE001
        log.warning("semantic_cache_store_failed", error=str(exc))


async def _prune_over_cap(db: AsyncSession) -> None:
    """Drop the oldest rows once the tenant's cache exceeds _MAX_ROWS.

    Oldest-first doubles as expired-first: TTL-dead rows (invisible to lookup but
    still occupying the table) are exactly the ones evicted.
    """
    total = (await db.execute(text("SELECT count(*) FROM semantic_cache"))).scalar_one()
    if total <= _MAX_ROWS:
        return
    await db.execute(
        text(
            """
            DELETE FROM semantic_cache WHERE id IN (
                SELECT id FROM semantic_cache ORDER BY created_at ASC LIMIT :n
            )
            """
        ),
        {"n": total - _MAX_ROWS},
    )
    log.info("semantic_cache_pruned", deleted=total - _MAX_ROWS)


async def invalidate_all(db: AsyncSession) -> int:
    """Delete every cached answer for the current tenant; returns the row count.

    Cached answers embed knowledge content and their citations point at chunk ids, so
    any knowledge edit/archive/delete must flush the cache — the lookup TTL is only
    the backstop. RLS scopes the bare DELETE to the caller's tenant. The caller owns
    the transaction (no commit here), mirroring store().
    """
    result = await db.execute(text("DELETE FROM semantic_cache"))
    deleted = result.rowcount or 0
    if deleted:
        log.info("semantic_cache_invalidated", deleted=deleted)
    return deleted
