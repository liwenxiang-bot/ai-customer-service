"""Embedding-model migration: full vector rebuild + degrade + progress.

Switching the embedding model invalidates all stored vectors (and may change the
dimension). We: (optionally) re-shape the vector column to the new dim and rebuild its
HNSW index, then re-embed every item, updating a progress row the admin UI polls.
During the rebuild, vector search naturally degrades (it filters by the active dim, so
not-yet-rebuilt chunks are skipped) while keyword search keeps working
(requirements §6 Embedding 迁移).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.enums import ChunkStatus, JobStatus, KnowledgeStatus
from app.models.knowledge import EmbeddingRebuildJob, KnowledgeChunk, KnowledgeItem
from app.services.knowledge import reembed_item

log = get_logger("embedding_rebuild")


async def start_rebuild(
    db: AsyncSession, from_model: str, from_dim: int, to_model: str, to_dim: int
) -> EmbeddingRebuildJob:
    job = EmbeddingRebuildJob(
        status=JobStatus.PENDING,
        from_model=from_model,
        to_model=to_model,
        from_dim=from_dim,
        to_dim=to_dim,
    )
    db.add(job)
    await db.flush()
    from app.tasks.queue import enqueue

    await enqueue("rebuild_embeddings", str(job.id))
    return job


async def latest_job(db: AsyncSession) -> EmbeddingRebuildJob | None:
    return (
        await db.execute(
            select(EmbeddingRebuildJob).order_by(EmbeddingRebuildJob.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def _alter_vector_dim(db: AsyncSession, new_dim: int) -> None:
    """Re-shape the embedding columns to a new dimension and rebuild HNSW indexes.
    All existing vectors are cleared (they're about to be regenerated)."""
    log.warning("altering_vector_dimension", new_dim=new_dim)
    await db.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
    await db.execute(text("DROP INDEX IF EXISTS ix_semcache_embedding_hnsw"))
    await db.execute(
        text(f"ALTER TABLE knowledge_chunks ALTER COLUMN embedding TYPE vector({new_dim}) USING NULL")
    )
    await db.execute(
        text(f"ALTER TABLE semantic_cache ALTER COLUMN embedding TYPE vector({new_dim}) USING NULL")
    )
    await db.execute(
        text("CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
             "USING hnsw (embedding vector_cosine_ops)")
    )
    await db.execute(
        text("CREATE INDEX ix_semcache_embedding_hnsw ON semantic_cache "
             "USING hnsw (embedding vector_cosine_ops)")
    )
    await db.commit()


async def run_rebuild(db: AsyncSession, job_id: str) -> None:
    job = await db.get(EmbeddingRebuildJob, uuid.UUID(job_id))
    if not job:
        return
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    await db.commit()

    try:
        if job.from_dim and job.to_dim and job.from_dim != job.to_dim:
            await _alter_vector_dim(db, job.to_dim)
        else:
            # Same dim, new model → just clear and rebuild content vectors.
            await db.execute(update(KnowledgeChunk).values(status=ChunkStatus.STALE))
            await db.commit()

        item_ids = (
            await db.execute(
                select(KnowledgeItem.id).where(KnowledgeItem.status != KnowledgeStatus.ARCHIVED)
            )
        ).scalars().all()

        job.total_chunks = (
            await db.execute(select(func.count(KnowledgeChunk.id)))
        ).scalar_one() or len(item_ids)
        await db.commit()

        # Re-embed items concurrently (bounded) — each worker on its own session so the
        # embedding-API latency overlaps instead of summing. The job session stays owned by
        # this coroutine, which advances progress as workers finish.
        sem = asyncio.Semaphore(max(1, settings.embedding_rebuild_concurrency))

        async def _one(iid) -> int:
            async with sem, session_scope() as s:
                return await reembed_item(s, str(iid))  # commits its own session

        processed = 0
        tasks = [asyncio.create_task(_one(i)) for i in item_ids]
        for fut in asyncio.as_completed(tasks):
            try:
                n = await fut
            except Exception as exc:  # noqa: BLE001 — one item failing must not abort the rebuild
                log.warning("rebuild_item_failed", error=str(exc))
                n = 0
            processed += max(n, 1)
            job.processed_chunks = processed
            job.progress = min(processed / job.total_chunks, 0.99) if job.total_chunks else 1.0
            await db.commit()

        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.finished_at = datetime.now(UTC)
        await db.commit()
        log.info("rebuild_completed", job_id=job_id, items=len(item_ids))
    except Exception as exc:  # noqa: BLE001
        log.error("rebuild_failed", job_id=job_id, error=str(exc))
        job.status = JobStatus.FAILED
        job.error = str(exc)[:1000]
        job.finished_at = datetime.now(UTC)
        await db.commit()
