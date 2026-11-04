"""ARQ worker: embedding, rebuild, summarization, distillation, cleanup.

Run with:  arq app.tasks.worker.WorkerSettings
All tasks are idempotent-ish and use their own DB session; ARQ provides retries.
"""

from __future__ import annotations

from arq import func
from sqlalchemy import text

from app.config import settings
from app.core.clock import app_tz
from app.core.logging import configure_logging, get_logger, set_trace_id
from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.tasks.queue import redis_settings
from app.tasks.wechat_tasks import notify_handoff, wechat_handle_inbound, wechat_send

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


async def distill_human_reply(ctx, session_id: str, _tenant: str | None = None) -> bool:
    """Handoff resolved / takeover ended → the operator's answer becomes a review candidate."""
    from app.services.distill import distill_latest_human_reply

    with tenant_scope(_tenant):
        async with session_scope() as db:
            return await distill_latest_human_reply(db, session_id)


async def suggest_questions(ctx, item_id: str, _tenant: str | None = None) -> int:
    """LLM-suggested customer phrasings for one knowledge item (rag.questions)."""
    import uuid

    from app.models.knowledge import KnowledgeItem
    from app.services.knowledge_questions import generate_questions

    with tenant_scope(_tenant):
        async with session_scope() as db:
            item = await db.get(KnowledgeItem, uuid.UUID(item_id))
            if item is None:
                return 0
            generated = await generate_questions(db, item)
            await db.commit()
            return len(generated)


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
    from app.services.embedding_rebuild import fail_stale_jobs

    try:
        with tenant_scope(DEFAULT_TENANT_ID):
            async with session_scope() as db:
                n = await fail_stale_jobs(db)
        if n:
            log.warning("stale_rebuild_jobs_failed", count=n)
    except Exception as exc:  # noqa: BLE001 — never block worker start
        log.warning("stale_rebuild_sweep_failed", error=str(exc))
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
        # A full rebuild of a large knowledge base can exceed the default 600s; a timeout kills
        # it mid-way and (before the stale sweep) left the job RUNNING forever.
        func(rebuild_embeddings, timeout=3 * 3600),
        summarize_session,
        distill_from_feedback,
        distill_human_reply,
        suggest_questions,
        cleanup_expired,
        reembed_stragglers,
        wechat_handle_inbound,
        wechat_send,
        notify_handoff,
    ]
    cron_jobs = _CRON
    redis_settings = redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown
    # Cron in the business timezone (the container runs UTC: 03:30 UTC = 11:30 Beijing peak).
    timezone = app_tz()
    max_jobs = 10
    job_timeout = 600
    keep_result = 3600
