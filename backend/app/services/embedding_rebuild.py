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
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.enums import ChunkStatus, JobStatus, KnowledgeStatus
from app.models.knowledge import EmbeddingRebuildJob, KnowledgeChunk, KnowledgeItem
from app.services.knowledge import reembed_item

log = get_logger("embedding_rebuild")


ACTIVE_JOB_WINDOW = timedelta(hours=3)  # = the worker's rebuild timeout


async def start_rebuild(
    db: AsyncSession, from_model: str, from_dim: int, to_model: str, to_dim: int
) -> EmbeddingRebuildJob:
    # Single-flight: a second click / config save while a rebuild is pending or running reuses
    # it instead of starting a concurrent full rebuild that fights over the same rows.
    active = (
        await db.execute(
            select(EmbeddingRebuildJob)
            .where(
                EmbeddingRebuildJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                EmbeddingRebuildJob.created_at >= datetime.now(UTC) - ACTIVE_JOB_WINDOW,
            )
            .order_by(EmbeddingRebuildJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None and active.to_model == to_model and active.to_dim == to_dim:
        log.info("rebuild_already_active", job_id=str(active.id))
        return active
    job = EmbeddingRebuildJob(
        status=JobStatus.PENDING,
        from_model=from_model,
        to_model=to_model,
        from_dim=from_dim,
        to_dim=to_dim,
    )
    db.add(job)
    await db.flush()
    from app.db.session import run_after_commit
    from app.tasks.queue import enqueue

    # Enqueue only once the job row is committed: a fast worker would otherwise load a job that
    # doesn't exist yet and leave it PENDING forever.
    jid = str(job.id)
    run_after_commit(db, lambda: enqueue("rebuild_embeddings", jid, _job_id=f"rebuild:{jid}"))
    return job


async def fail_stale_jobs(db: AsyncSession) -> int:
    """Called at worker start: jobs left RUNNING/PENDING by a crash or timeout would show as
    「重建中」 forever and block new rebuilds — mark them failed with a clear reason."""
    res = await db.execute(
        update(EmbeddingRebuildJob)
        .where(
            EmbeddingRebuildJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            EmbeddingRebuildJob.created_at < datetime.now(UTC) - ACTIVE_JOB_WINDOW,
        )
        .values(status=JobStatus.FAILED, error="重建被中断（worker 重启或超时），请重新发起")
    )
    return res.rowcount or 0


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
    # Explicit build params (defaults are m=16/ef_construction=64): construction-time
    # edge quality caps recall at scale and can't be recovered via ef_search. Matches
    # migration 0015. semantic_cache keeps defaults — a miss only costs one LLM call.
    await db.execute(
        text("CREATE INDEX ix_chunks_embedding_hnsw ON knowledge_chunks "
             "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200)")
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
        # Either path invalidates every stored vector, so mark all chunks STALE. The
        # dim-change branch must not skip this: _alter_vector_dim NULLs the vectors but
        # used to leave status='ready' — if the rebuild was then interrupted (worker
        # restart, arq timeout), those ready+NULL rows were invisible to the closing
        # audit, reembed-pending and the straggler cron (fake-ready recall gap).
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
        failed_items = 0

        async def _one(iid) -> int:
            # Failures are caught here (not at the as_completed loop) so the log line
            # can carry the item_id — otherwise a failed item can only be found by a
            # full re-run instead of a targeted per-item reembed.
            nonlocal failed_items
            try:
                async with sem, session_scope() as s:
                    return await reembed_item(s, str(iid))  # commits its own session
            except Exception as exc:  # noqa: BLE001 — one item failing must not abort the rebuild
                failed_items += 1
                log.warning("rebuild_item_failed", item_id=str(iid), error=str(exc))
                return 0

        processed = 0
        tasks = [asyncio.create_task(_one(i)) for i in item_ids]
        for fut in asyncio.as_completed(tasks):
            n = await fut
            processed += max(n, 1)
            job.processed_chunks = processed
            job.progress = min(processed / job.total_chunks, 0.99) if job.total_chunks else 1.0
            await db.commit()

        # Audit the DB instead of trusting the loop: per-item failures are caught above,
        # but embedding-API failures (rate limits during a full rebuild are common) are
        # swallowed inside reembed_item, which stores unembedded PENDING chunks and
        # returns normally. Any chunk left not-ready is an invisible recall gap unless
        # we surface it here.
        # `embedding IS NULL` widens the net beyond status: a rebuild interrupted between
        # the vector-clearing DDL and the STALE flip can leave ready+NULL rows.
        not_ready = (
            await db.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    or_(
                        KnowledgeChunk.status != ChunkStatus.READY,
                        KnowledgeChunk.embedding.is_(None),
                    )
                )
            )
        ).scalar_one()
        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.error = (
            f"重建完成，但 {not_ready} 个分块未生成向量（嵌入失败或限流），"
            "相关内容暂时只能靠关键词检索命中；可调用知识库的 reembed-pending 接口补齐"
            if not_ready
            else ""
        )
        job.finished_at = datetime.now(UTC)
        await db.commit()
        if not_ready:
            log.warning(
                "rebuild_completed_with_gaps",
                job_id=job_id,
                not_ready_chunks=not_ready,
                failed_items=failed_items,
            )
        log.info("rebuild_completed", job_id=job_id, items=len(item_ids), failed_items=failed_items)
    except Exception as exc:  # noqa: BLE001
        log.error("rebuild_failed", job_id=job_id, error=str(exc))
        job.status = JobStatus.FAILED
        job.error = str(exc)[:1000]
        job.finished_at = datetime.now(UTC)
        await db.commit()
