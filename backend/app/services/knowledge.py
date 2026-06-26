"""Knowledge ingestion + CRUD with versioning and audit.

Save flow: persist item → snapshot a version → enqueue async embedding (chunk → embed →
FTS). Vectors are generated off the request path (requirements §6, §7, §13).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import estimate_tokens
from app.core.logging import get_logger
from app.llm.factory import get_embedding_client
from app.models.enums import ChunkStatus, KnowledgeStatus
from app.models.knowledge import KnowledgeChunk, KnowledgeItem, KnowledgeVersion
from app.rag.chunking import chunk_text
from app.rag.segment import segment
from app.services.ai_config import get_active_ai_config, to_embedding_settings
from app.services.audit import write_audit

log = get_logger("knowledge")


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
        version=1,
    )
    db.add(item)
    await db.flush()
    await _snapshot_version(db, item, actor, "created")
    await write_audit(db, actor, "knowledge.create", "knowledge_item", str(item.id), {"title": item.title})
    await _enqueue_embed(item.id)
    return item


async def update_item(db: AsyncSession, item: KnowledgeItem, data: dict, actor=None) -> KnowledgeItem:
    content_changed = (
        data.get("content", item.content) != item.content
        or data.get("title", item.title) != item.title
    )
    # Snapshot the pre-edit state, then bump version.
    await _snapshot_version(db, item, actor, data.get("change_note", "edited"))
    item.version += 1
    for field in ("title", "content", "category", "tags", "status"):
        if field in data:
            setattr(item, field, data[field])
    await db.flush()
    await write_audit(db, actor, "knowledge.update", "knowledge_item", str(item.id), {"version": item.version})
    if content_changed:
        await _enqueue_embed(item.id)
    return item


async def delete_item(db: AsyncSession, item: KnowledgeItem, actor=None) -> None:
    await write_audit(db, actor, "knowledge.delete", "knowledge_item", str(item.id), {"title": item.title})
    await db.delete(item)
    await db.flush()


async def rollback_to_version(db: AsyncSession, item: KnowledgeItem, version_id: str, actor=None):
    ver = await db.get(KnowledgeVersion, uuid.UUID(version_id))
    if not ver or ver.item_id != item.id:
        return None
    await update_item(
        db, item,
        {"title": ver.title, "content": ver.content, "category": ver.category,
         "tags": ver.tags, "change_note": f"rollback to v{ver.version}"},
        actor,
    )
    return item


async def reembed_item(db: AsyncSession, item_id: str) -> int:
    """Chunk an item, generate embeddings, and (re)build its chunks. Returns chunk count.
    Runs in the worker. If embeddings are unavailable, chunks are stored unembedded
    (keyword search still works) and marked pending."""
    item = await db.get(KnowledgeItem, uuid.UUID(item_id) if isinstance(item_id, str) else item_id)
    if not item or item.status == KnowledgeStatus.ARCHIVED:
        # Archived/deleted → drop chunks so they stop being retrieved.
        if item:
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))
            await db.commit()
        return 0

    ai_config = await get_active_ai_config(db)
    emb_cfg = to_embedding_settings(ai_config)
    params = ai_config.retrieval or {}
    text = f"{item.title}\n{item.content}" if item.title else item.content
    pieces = chunk_text(text, int(params.get("chunk_size", 600)), int(params.get("chunk_overlap", 100)))

    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.item_id == item.id))

    vectors: list = [None] * len(pieces)
    if emb_cfg.api_key and pieces:
        try:
            vectors = await get_embedding_client(emb_cfg).embed(pieces)
        except Exception as exc:  # noqa: BLE001
            log.warning("embed_failed_storing_unembedded", item_id=str(item.id), error=str(exc))
            vectors = [None] * len(pieces)

    for i, (piece, vec) in enumerate(zip(pieces, vectors, strict=False)):
        db.add(
            KnowledgeChunk(
                tenant_id=item.tenant_id,
                item_id=item.id,
                chunk_index=i,
                content=piece,
                content_seg=segment(piece),
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


async def test_retrieval(db: AsyncSession, query: str) -> list[dict]:
    """Used by the editor's 'test retrieval' feature."""
    from app.rag.retrieval import hybrid_search

    ai_config = await get_active_ai_config(db)
    results = await hybrid_search(db, ai_config, query)
    return [
        {"item_id": r.item_id, "chunk_id": r.chunk_id, "title": r.title,
         "snippet": r.content[:300], "score": r.score}
        for r in results
    ]


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
                "count(*) FILTER (WHERE status = 'ready') AS ready "
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


async def items_needing_embedding(db: AsyncSession) -> list[str]:
    """Non-archived items with content whose chunks are missing or not all 'ready' — the
    set to re-embed after an embedding outage or the long-doc batch fix."""
    rows = (
        await db.execute(
            text(
                """
                SELECT i.id::text FROM knowledge_items i
                WHERE i.status <> 'archived' AND i.content <> ''
                  AND (NOT EXISTS (SELECT 1 FROM knowledge_chunks c WHERE c.item_id = i.id)
                       OR EXISTS (SELECT 1 FROM knowledge_chunks c
                                  WHERE c.item_id = i.id AND c.status <> 'ready'))
                """
            )
        )
    ).all()
    return [r[0] for r in rows]


async def enqueue_reembed(item_id: str) -> None:
    """Public wrapper so the admin API can (re)queue an item's embedding."""
    await _enqueue_embed(item_id)


async def _enqueue_embed(item_id) -> None:
    from app.tasks.queue import enqueue

    await enqueue("embed_item", str(item_id))
