"""User feedback (👍/👎) on assistant messages — a knowledge-distillation signal."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.conversation import Message as DBMessage
from app.models.enums import FeedbackKind

log = get_logger("feedback")


async def set_feedback(
    db: AsyncSession, message_id: str, kind: str, note: str = ""
) -> bool:
    try:
        mid = uuid.UUID(message_id)
    except (ValueError, TypeError):
        return False
    msg = await db.get(DBMessage, mid)
    if not msg:
        return False
    if kind not in (FeedbackKind.UP, FeedbackKind.DOWN):
        return False
    msg.feedback = kind
    msg.feedback_note = note or ""
    await db.flush()

    # 👎 is the strongest live "retrieval failed the user" signal. Log it structured —
    # with whether a retrieval actually backed the answer — so dashboards can split
    # 漏召回 from chit-chat downvotes and scripts/mine_bad_cases has a cheap audit trail.
    if kind == FeedbackKind.DOWN:
        has_retrieval = any(
            isinstance(tc, dict) and tc.get("name") == "search_knowledge"
            for tc in (msg.tool_calls or [])
        )
        log.info(
            "feedback_down",
            message_id=str(msg.id),
            session_id=str(msg.session_id),
            has_retrieval=has_retrieval,
            citation_count=len(msg.citations or []),
            note=(note or "")[:200],
        )

    # 👍 on a grounded answer → candidate for knowledge distillation (best-effort).
    if kind == FeedbackKind.UP and msg.citations:
        try:
            from app.tasks.queue import enqueue

            await enqueue("distill_from_feedback", str(msg.id))
        except Exception:  # noqa: BLE001
            pass
    return True
