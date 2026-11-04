"""Stamp messages.answer_kind on historical assistant messages (idempotent, per tenant).

Rows written before 0020 have answer_kind = "". The dashboard's effective hit rate and
the knowledge-gap list only see classified rows, so run this once after deploying:

    docker compose exec -T backend python -m scripts.backfill_answer_kind [--dry-run]

Rules mirror the runtime (services.answer_kind.classify) plus what the runtime knows
from context and this script must infer from the stored trace:
  - model == 'human'                         → human (operator bubble)
  - degraded flag / the empty-reply fallback → degraded
  - decline_off_topic in the trace or the configured off-topic reply text → off_topic
  - escalate_to_human in the trace           → handoff
  - otherwise classify() over the preceding customer message, the answer text and the
    citations the answer actually references (clean_citations)
Safe under RLS: iterates tenants and pins each one; only touches rows with answer_kind = "".
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select, text

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.db.tenant_context import tenant_scope
from app.models.tenant import Tenant
from app.services.abuse_guard import DEFAULT_OFF_TOPIC_REPLY
from app.services.ai_config import get_active_ai_config
from app.services.answer_kind import classify, clean_citations

log = get_logger("backfill_answer_kind")

_BATCH = 500
_EMPTY_FALLBACK = "抱歉，我这边没能正常生成回复"
_DEGRADED_PREFIX = "抱歉，我这边暂时遇到了一点问题"

_SELECT = text(
    """
    SELECT m.id, m.content, m.citations, m.tool_calls, m.degraded, m.model,
           (SELECT u.content FROM messages u
             WHERE u.session_id = m.session_id AND u.role = 'user' AND u.seq < m.seq
             ORDER BY u.seq DESC LIMIT 1) AS question
    FROM messages m
    WHERE m.role = 'assistant' AND m.answer_kind = ''
    ORDER BY m.created_at
    LIMIT :limit
    """
)


def infer_kind(row, off_topic_texts: set[str]) -> str:
    _id, content, citations, tool_calls, degraded, model, question = row
    content = content or ""
    tools = tool_calls or []
    names = {t.get("name") for t in tools if isinstance(t, dict)}
    if model == "human":
        return "human"
    if degraded or content.startswith(_EMPTY_FALLBACK) or content.startswith(_DEGRADED_PREFIX):
        return "degraded"
    if "decline_off_topic" in names or content.strip() in off_topic_texts:
        return "off_topic"
    if "escalate_to_human" in names:
        return "handoff"
    return classify(
        user_text=question or "",
        final_text=content,
        citations=clean_citations(content, citations or []),
        searched="search_knowledge" in names,
    )


async def backfill_tenant(tenant_id, dry_run: bool) -> dict:
    counts: dict[str, int] = {}
    with tenant_scope(tenant_id):
        async with session_scope() as db:
            cfg = await get_active_ai_config(db)
            off_topic_texts = {DEFAULT_OFF_TOPIC_REPLY.strip(), (cfg.off_topic_reply or "").strip()} - {""}
            while True:
                rows = (await db.execute(_SELECT, {"limit": _BATCH})).all()
                if not rows:
                    break
                updates = [{"id": r[0], "kind": infer_kind(r, off_topic_texts)} for r in rows]
                for u in updates:
                    counts[u["kind"]] = counts.get(u["kind"], 0) + 1
                if dry_run:
                    # Without writing, the same rows would come back forever.
                    log.info("dry_run_batch", tenant=str(tenant_id), rows=len(rows), counts=counts)
                    if len(rows) < _BATCH:
                        break
                    return counts | {"_note": "dry-run stops after the first batch"}
                await db.execute(
                    text("UPDATE messages SET answer_kind = :kind WHERE id = :id"), updates
                )
                await db.commit()
                log.info("batch_done", tenant=str(tenant_id), rows=len(rows))
            if dry_run:
                await db.rollback()
    return counts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="classify and count, write nothing")
    args = parser.parse_args()
    configure_logging()
    async with session_scope() as db:  # tenants is not tenant-scoped
        tenant_ids = [tid for (tid,) in (await db.execute(select(Tenant.id))).all()]
    total: dict[str, int] = {}
    for tid in tenant_ids:
        counts = await backfill_tenant(tid, args.dry_run)
        print(f"tenant {tid}: {counts}")
        for k, v in counts.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    print(("DRY-RUN " if args.dry_run else "") + f"total: {total}")


if __name__ == "__main__":
    asyncio.run(main())
