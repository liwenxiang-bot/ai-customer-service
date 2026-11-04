"""Knowledge ingestion + CRUD with versioning and audit.

Save flow: persist item → snapshot a version → enqueue async embedding (chunk → embed →
FTS). Vectors are generated off the request path (requirements §6, §7, §13).
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import estimate_tokens
from app.core.logging import get_logger
from app.db.tenant_context import get_current_tenant
from app.llm.factory import get_embedding_client
from app.models.enums import ChunkStatus, KnowledgeStatus
from app.models.knowledge import KnowledgeChunk, KnowledgeItem, KnowledgeVersion
from app.rag.chunking import Chunk, chunk_markdown
from app.rag.segment import segment
from app.services.ai_config import get_active_ai_config, to_embedding_settings
from app.services.audit import write_audit

log = get_logger("knowledge")


# Cap the content copy stored in the audit detail: it is the last recoverable copy
# after a delete (versions CASCADE away with the item), but one giant document must
# not bloat the audit table.
_AUDIT_CONTENT_MAX = 50_000

# Fields update_item applies from `data`; also drives the changed_fields audit entry.
_UPDATABLE_FIELDS = ("title", "content", "category", "tags", "status", "effective_from", "expires_at")


async def _snapshot_version(db: AsyncSession, item: KnowledgeItem, actor, note: str) -> None:
    db.add(
        KnowledgeVersion(
            tenant_id=item.tenant_id,
            item_id=item.id,
            version=item.version,
            title=item.title,
            content=item.content,
            category=item.category,
            tags=item.tags,
            status=item.status,
            effective_from=item.effective_from,
            expires_at=item.expires_at,
            editor_id=getattr(actor, "id", None),
            editor_email=getattr(actor, "email", ""),
            change_note=note,
        )
    )


async def create_item(db: AsyncSession, data: dict, actor=None) -> KnowledgeItem:
    item = KnowledgeItem(
        title=data.get("title", ""),
        content=data.get("content", ""),
        category=data.get("category", ""),
        tags=data.get("tags", []),
        status=data.get("status", KnowledgeStatus.PUBLISHED),
        source=data.get("source", "manual"),
        # Validity window arrives on import rows; the admin API sets it on the flushed
        # row instead, so absent keys simply stay None here.
        effective_from=data.get("effective_from"),
        expires_at=data.get("expires_at"),
        # Provenance for document/URL imports (source_url, source_name, section); lets
        # a re-import of the same URL update the item instead of duplicating it.
        extra=dict(data.get("extra") or {}),
        version=1,
    )
    db.add(item)
    await db.flush()
    await _snapshot_version(db, item, actor, "created")
    await write_audit(db, actor, "knowledge.create", "knowledge_item", str(item.id), {"title": item.title})
    await _purge_answer_cache(db)
    await _enqueue_embed(db, item.id)
    return item


async def update_item(db: AsyncSession, item: KnowledgeItem, data: dict, actor=None) -> KnowledgeItem:
    changed_fields = [
        f for f in _UPDATABLE_FIELDS if f in data and getattr(item, f) != data[f]
    ]
    if not changed_fields:
        # No-op edit (e.g. bulk status-set hitting items already in that status):
        # don't bump the version, flood the snapshot table, or re-embed.
        return item
    content_changed = "content" in changed_fields or "title" in changed_fields
    # Snapshot the pre-edit state, then bump version.
    await _snapshot_version(db, item, actor, data.get("change_note", "edited"))
    item.version += 1
    for field in changed_fields:
        setattr(item, field, data[field])
    await db.flush()
    await write_audit(
        db, actor, "knowledge.update", "knowledge_item", str(item.id),
        {"version": item.version, "changed_fields": changed_fields},
    )
    await _purge_answer_cache(db)
    if content_changed:
        await _enqueue_embed(db, item.id)
    return item


async def delete_item(db: AsyncSession, item: KnowledgeItem, actor=None) -> None:
    # Full snapshot into the audit detail — versions are CASCADE-deleted with the item,
    # so this is the only copy left for manually recovering a bad delete.
    await write_audit(
        db, actor, "knowledge.delete", "knowledge_item", str(item.id),
        {
            "title": item.title,
            "content": item.content[:_AUDIT_CONTENT_MAX],
            "content_truncated": len(item.content) > _AUDIT_CONTENT_MAX,
            "category": item.category,
            "tags": item.tags,
            "version": item.version,
        },
    )
    await db.delete(item)
    await db.flush()
    await _purge_answer_cache(db)


async def rollback_to_version(db: AsyncSession, item: KnowledgeItem, version_id: str, actor=None):
    ver = await db.get(KnowledgeVersion, uuid.UUID(version_id))
    if not ver or ver.item_id != item.id:
        return None
    data = {"title": ver.title, "content": ver.content, "category": ver.category,
            "tags": ver.tags, "change_note": f"rollback to v{ver.version}"}
    if ver.status is not None:
        # Snapshots taken before migration 0016 never recorded status/window (status is
        # NULL there); keep the item's current values for those instead of blanking.
        data.update(status=ver.status, effective_from=ver.effective_from, expires_at=ver.expires_at)
    await update_item(db, item, data, actor)
    return item


async def reembed_item(db: AsyncSession, item_id: str) -> int:
    """Chunk an item, generate embeddings, and (re)build its chunks. Returns chunk count.
    Runs in the worker. If embeddings are unavailable, chunks are stored unembedded
    (keyword search still works) and marked pending."""
    iid = uuid.UUID(item_id) if isinstance(item_id, str) else item_id
    # Concurrent jobs for the same item (rapid edits, manual requeue + straggler cron)
    # serialize here; each rebuilds from the row it reads AFTER its predecessor commits,
    # so the delete+insert below can't interleave into duplicate chunks. Deliberately
    # NOT deduped via arq job ids: with keep_result on, a same-id enqueue is silently
    # dropped for an hour after success — a lost re-embed is worse than a wasted one.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended('embed:' || :iid, 0))"),
        {"iid": str(iid)},
    )
    item = await db.get(KnowledgeItem, iid)
    if not item or item.status == KnowledgeStatus.ARCHIVED:
        # Archived/deleted → drop chunks so they stop being retrieved.
        if item:
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))
            await db.commit()
        return 0

    ai_config = await get_active_ai_config(db)
    emb_cfg = to_embedding_settings(ai_config)
    params = ai_config.retrieval or {}
    chunks = chunk_markdown(
        item.content, int(params.get("chunk_size", 600)), int(params.get("chunk_overlap", 100))
    )

    # Anchor every chunk to its document: the title — and the chunk's heading path
    # inside the document ("安装 > 第三步") — join the embedding and FTS inputs so
    # heading-style queries reach chunks deep in a long document, while `content`
    # stays the raw slice for citation accuracy. Tags are keyword-ish noise for the
    # vector space, so they only join the FTS side.
    anchor = (item.title or "").strip()
    tags_line = " ".join(str(t) for t in (item.tags or []) if t).strip()
    fts_head = "\n".join(p for p in (anchor, tags_line) if p)
    if not chunks and anchor:
        chunks = [Chunk(anchor)]  # title-only item: keep it retrievable
    pieces = [c.text for c in chunks]

    def _head(c: Chunk) -> str:
        parts = [anchor] + ([c.heading] if c.heading and c.heading != anchor else [])
        return "\n".join(p for p in parts if p)

    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))

    vectors: list = [None] * len(pieces)
    if emb_cfg.api_key and pieces:
        try:
            vectors = await get_embedding_client(emb_cfg).embed(
                [f"{_head(c)}\n{c.text}" if _head(c) else c.text for c in chunks]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("embed_failed_storing_unembedded", item_id=str(item.id), error=str(exc))
            vectors = [None] * len(pieces)

    for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=False)):
        piece = chunk.text
        seg_head = "\n".join(p for p in (fts_head, chunk.heading) if p)
        db.add(
            KnowledgeChunk(
                tenant_id=item.tenant_id,
                item_id=item.id,
                chunk_index=i,
                content=piece,
                content_seg=segment(f"{seg_head}\n{piece}" if seg_head else piece),
                embedding=vec,
                embedding_model=emb_cfg.model if vec is not None else "",
                embedding_dim=emb_cfg.dim if vec is not None else 0,
                status=ChunkStatus.READY if vec is not None else ChunkStatus.PENDING,
                token_count=estimate_tokens(piece),
            )
        )
    await db.commit()
    log.info("item_embedded", item_id=str(item.id), chunks=len(pieces), embedded=bool(emb_cfg.api_key))
    return len(pieces)


async def test_retrieval(db: AsyncSession, query: str) -> dict:
    """Used by the editor's 'test retrieval' feature.

    fusion (RRF, ≈0.01–0.05) and rerank (≈0–1) scores live on different scales; the
    batch-wide score_kind lets the UI label which one it is showing. None ⇔ no results.
    """
    from app.rag.retrieval import hybrid_search

    ai_config = await get_active_ai_config(db)
    results = await hybrid_search(db, ai_config, query)
    return {
        "score_kind": results[0].score_kind if results else None,
        "results": [
            {"item_id": r.item_id, "chunk_id": r.chunk_id, "title": r.title,
             "snippet": r.content[:300], "score": r.score}
            for r in results
        ],
    }


async def count_chunks(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(KnowledgeChunk.id)))).scalar_one()


async def chunk_coverage(db: AsyncSession, item_ids: list) -> dict[str, tuple[int, int]]:
    """{item_id: (total_chunks, ready_chunks)} for a page of items — so the list view can
    flag items whose vectors are missing/incomplete (e.g. an embed batch that failed)."""
    if not item_ids:
        return {}
    rows = (
        await db.execute(
            text(
                "SELECT item_id::text, count(*) AS total, "
                # embedding IS NOT NULL guards against status drift (a crashed job that
                # marked ready before writing the vector) — ready must mean retrievable.
                "count(*) FILTER (WHERE status = 'ready' AND embedding IS NOT NULL) AS ready "
                "FROM knowledge_chunks WHERE item_id = ANY(:ids) GROUP BY item_id"
            ),
            {"ids": [str(i) for i in item_ids]},
        )
    ).all()
    return {r[0]: (int(r[1]), int(r[2])) for r in rows}


async def list_item_chunks(db: AsyncSession, item_id: str) -> list[dict]:
    """Per-chunk detail for one item (no vector payload pulled) — lets the editor see how a
    document was split and which chunks lack an embedding."""
    rows = (
        await db.execute(
            text(
                "SELECT id::text, chunk_index, content, status, token_count, "
                "(embedding IS NOT NULL) AS embedded, embedding_model, embedding_dim "
                "FROM knowledge_chunks WHERE item_id = :iid ORDER BY chunk_index"
            ),
            {"iid": item_id},
        )
    ).all()
    return [
        {
            "id": r[0], "chunk_index": r[1], "content": r[2], "status": r[3],
            "token_count": r[4], "embedded": bool(r[5]),
            "embedding_model": r[6], "embedding_dim": r[7],
        }
        for r in rows
    ]


async def items_needing_embedding(db: AsyncSession, min_age_minutes: int = 0) -> list[str]:
    """Non-archived items with content whose chunks are missing or not all 'ready' — the
    set to re-embed after an embedding outage or the long-doc batch fix.

    `min_age_minutes` skips recently-touched items whose embed job is likely still in
    flight — the straggler cron uses it to avoid double-firing on healthy items."""
    rows = (
        await db.execute(
            text(
                """
                SELECT i.id::text FROM knowledge_items i
                WHERE i.status <> 'archived' AND i.content <> ''
                  AND i.updated_at < now() - make_interval(mins => :age)
                  AND (NOT EXISTS (SELECT 1 FROM knowledge_chunks c WHERE c.item_id = i.id)
                       OR EXISTS (SELECT 1 FROM knowledge_chunks c
                                  WHERE c.item_id = i.id
                                    AND (c.status <> 'ready' OR c.embedding IS NULL)))
                """
            ),
            {"age": min_age_minutes},
        )
    ).all()
    return [r[0] for r in rows]


async def enqueue_reembed(item_id: str) -> None:
    """Public wrapper so the admin API can (re)queue an already-committed item."""
    from app.tasks.queue import enqueue

    await enqueue("embed_item", str(item_id))


async def _purge_answer_cache(db: AsyncSession) -> None:
    """Knowledge changed → cached answers may now cite stale content; drop them all.
    Coarse but correct: entries are cheap to rebuild and RLS scopes the wipe to the
    current tenant."""
    from app.services.semantic_cache import invalidate_all

    try:
        await invalidate_all(db)
    except Exception as exc:  # noqa: BLE001 — cache hygiene must never block a save
        log.warning("semantic_cache_invalidate_failed", error=str(exc))


async def _enqueue_embed(db: AsyncSession, item_id) -> None:
    """Fire the embed job only after the surrounding transaction commits: the worker
    opens its own session, so an early enqueue races it against uncommitted rows —
    it would find nothing (new item, arq gives up) or read stale content and mark
    the old vectors ready."""
    from app.tasks.queue import enqueue

    iid = str(item_id)
    tenant = get_current_tenant()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — no loop, nothing to defer onto
        await enqueue("embed_item", iid)
        return

    @event.listens_for(db.sync_session, "after_commit", once=True)
    def _fire(_session) -> None:
        # Runs in the engine greenlet on the loop's thread; the task is scheduled onto
        # the loop with the tenant captured above (the request context may be gone by
        # the time it executes).
        loop.create_task(enqueue("embed_item", iid, _tenant=str(tenant) if tenant else None))
