"""Auto knowledge distillation: turn good Q&A into review candidates (requirements §7, §18 P3).

Triggered by 👍 on a grounded answer (or batch over conversations). An LLM extracts a
reusable Q&A; a human approves it in the review queue before it enters the KB.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.llm.factory import get_provider
from app.models.conversation import Message as DBMessage
from app.models.enums import MessageRole, ReviewStatus
from app.models.knowledge import KnowledgeReviewCandidate
from app.services.ai_config import get_active_ai_config, to_llm_settings

log = get_logger("distill")


async def distill_from_message(db: AsyncSession, message_id: str) -> bool:
    """Build a review candidate from an assistant message + its preceding user question."""
    msg = await db.get(DBMessage, uuid.UUID(message_id))
    if not msg or msg.role != MessageRole.ASSISTANT:
        return False

    # Already distilled? Avoid duplicates.
    dup = (
        await db.execute(
            select(KnowledgeReviewCandidate.id).where(
                KnowledgeReviewCandidate.source_message_id == msg.id
            ).limit(1)
        )
    ).scalar_one_or_none()
    if dup:
        return False

    question = (
        await db.execute(
            select(DBMessage)
            .where(
                DBMessage.session_id == msg.session_id,
                DBMessage.seq < msg.seq,
                DBMessage.role == MessageRole.USER,
            )
            .order_by(DBMessage.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not question:
        return False

    ai_config = await get_active_ai_config(db)
    provider = get_provider(to_llm_settings(ai_config))
    prompt = [
        {"role": "system", "content": "你是知识库编辑。根据一问一答，提炼出可复用的标题(title)和正文(content)知识条目。"
         "正文要客观、去除对话语气、可独立阅读。仅返回 JSON：{\"title\":\"...\",\"content\":\"...\",\"category\":\"...\"}。"},
        {"role": "user", "content": f"问：{question.content}\n答：{msg.content}"},
    ]
    try:
        result = await provider.complete(prompt, max_tokens=600)
        import json

        text = result.text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("distill_parse_failed", error=str(exc))
        return False

    db.add(
        KnowledgeReviewCandidate(
            tenant_id=msg.tenant_id,
            source_session_id=msg.session_id,
            source_message_id=msg.id,
            raw_excerpt=f"问：{question.content}\n答：{msg.content}"[:2000],
            suggested_title=data.get("title", "")[:500],
            suggested_content=data.get("content", ""),
            suggested_category=data.get("category", ""),
            status=ReviewStatus.PENDING,
        )
    )
    await db.commit()
    log.info("distilled_candidate", message_id=message_id)
    return True
