"""ARQ worker: embedding, rebuild, summarization, distillation, cleanup.

Run with:  arq app.tasks.worker.WorkerSettings
All tasks are idempotent-ish and use their own DB session; ARQ provides retries.
"""

from __future__ import annotations

from sqlalchemy import text

from app.config import settings
from app.core.logging import configure_logging, get_logger, set_trace_id
from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.tasks.queue import redis_settings

log = get_logger("worker")


async def embed_item(ctx, item_id: str, _tenant: str | None = None) -> int:
    from app.services.knowledge import reembed_item

    with tenant_scope(_tenant):
        async with session_scope() as db:
            return await reembed_item(db, item_id)


async def rebuild_embeddings(ctx, job_id: str, _tenant: str | None = None) -> None:
    from app.services.embedding_rebuild import run_rebuild

    with tenant_scope(_tenant):
        async with session_scope() as db:
            await run_rebuild(db, job_id)


async def summarize_session(ctx, session_id: str, _tenant: str | None = None) -> bool:
    from app.services.summarize import summarize_session as _s

    with tenant_scope(_tenant):
        async with session_scope() as db:
            return await _s(db, session_id)


async def distill_from_feedback(ctx, message_id: str, _tenant: str | None = None) -> bool:
    from app.services.distill import distill_from_message

    with tenant_scope(_tenant):
        async with session_scope() as db:
            return await distill_from_message(db, message_id)


async def cleanup_expired(ctx, _tenant: str | None = None) -> int:
    """Cron, cross-tenant: clean each active tenant under its own RLS context."""
    from app.services.cleanup import cleanup_expired as _c

    with tenant_scope(DEFAULT_TENANT_ID):
        async with session_scope() as db:
            tids = (await db.execute(text("SELECT id FROM tenants WHERE is_active"))).scalars().all()
    total = 0
    for tid in tids:
        with tenant_scope(tid):
            async with session_scope() as db:
                total += await _c(db)
    return total


# Per-round requeue ceiling for the straggler cron: each requeue deletes and rebuilds the
# item's chunks, so an unbounded round during a persistent embedding outage keeps churning
# the whole broken set every 10 minutes (DB write amplification, burned API quota).
# Leftovers are picked up by the next round once the earlier ones succeed.
_STRAGGLER_REQUEUE_CAP = 50


async def reembed_stragglers(ctx, _tenant: str | None = None) -> int:
    """Cron, cross-tenant: requeue items whose chunks are stuck pending/missing.

    Self-heals the ways an item silently drops out of vector retrieval: an embedding
    outage left pending chunks, the enqueue was lost to a Redis blip, or a worker died
    mid-job. The age guard skips items whose embed job is likely still in flight.
    Skips tenants with no usable embedding credentials (requeueing would only churn
    chunks without ever producing vectors) and caps each round's requeues."""
    from app.services.ai_config import get_active_ai_config, to_embedding_settings
    from app.services.knowledge import items_needing_embedding
    from app.tasks.queue import enqueue

    with tenant_scope(DEFAULT_TENANT_ID):
        async with session_scope() as db:
            tids = (await db.execute(text("SELECT id FROM tenants WHERE is_active"))).scalars().all()
    total = 0
    for tid in tids:
        with tenant_scope(tid):
            async with session_scope() as db:
                cfg = await get_active_ai_config(db)
                if not to_embedding_settings(cfg).api_key:
                    log.warning(
                        "reembed_stragglers_skipped",
                        tenant=str(tid),
                        reason="embedding_api_key_missing",
                    )
                    continue
                ids = await items_needing_embedding(db, min_age_minutes=15)
            budget = _STRAGGLER_REQUEUE_CAP - total
            if len(ids) > budget:
                log.warning(
                    "reembed_stragglers_capped",
                    tenant=str(tid),
                    pending=len(ids),
                    cap=_STRAGGLER_REQUEUE_CAP,
                )
                ids = ids[:budget]
            for iid in ids:
                await enqueue("embed_item", iid, _tenant=str(tid))
            total += len(ids)
            if total >= _STRAGGLER_REQUEUE_CAP:
                break
    if total:
        log.info("reembed_stragglers_requeued", items=total)
    return total


async def on_startup(ctx) -> None:
    configure_logging()
    set_trace_id("worker")
    settings.enforce_secure_defaults()
    log.info("worker_started")


async def on_shutdown(ctx) -> None:
    from app.core.redis_client import close_redis
    from app.llm.factory import close_all

    await close_redis()
    await close_all()
    log.info("worker_stopped")


# Cron: nightly data-retention cleanup at 03:30; embed self-heal every 10 minutes.
try:
    from arq import cron

    _CRON = [
        cron(cleanup_expired, hour=3, minute=30),
        cron(reembed_stragglers, minute={5, 15, 25, 35, 45, 55}),
    ]
except Exception:  # pragma: no cover
    _CRON = []


class WorkerSettings:
    functions = [
        embed_item,
        rebuild_embeddings,
        summarize_session,
        distill_from_feedback,
        cleanup_expired,
        reembed_stragglers,
    ]
    cron_jobs = _CRON
    redis_settings = redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_jobs = 10
    job_timeout = 600
    keep_result = 3600
