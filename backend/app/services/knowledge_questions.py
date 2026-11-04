"""Question variants for knowledge items: LLM suggestion, manual append, bulk fill.

See rag.questions for why. Generation is one small LLM call per item; it runs
synchronously for the editor's "生成问法" button and in the worker for the bulk
"为缺问法的条目生成" action and for new manual items.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.llm.factory import get_provider
from app.models.enums import KnowledgeStatus
from app.models.knowledge import KnowledgeItem
from app.rag.questions import GENERATE_SYSTEM_PROMPT, normalize_questions, parse_generated
from app.services.ai_config import get_active_ai_config, to_llm_settings
from app.services.knowledge import update_item

log = get_logger("knowledge_questions")

_BULK_CAP = 300
_CONTENT_CHARS = 1500


async def generate_questions(db: AsyncSession, item: KnowledgeItem, actor=None) -> list[str]:
    """Ask the LLM for customer phrasings, store them as questions_auto (replacing the
    previous suggestions, never the hand-written list) and re-embed. Returns the list."""
    ai_config = await get_active_ai_config(db)
    settings_ = to_llm_settings(ai_config)
    if not settings_.api_key:
        raise RuntimeError("未配置 LLM API Key")
    provider = get_provider(settings_)
    prompt = [
        {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
        {"role": "user", "content": f"标题：{item.title}\n正文：\n{(item.content or '')[:_CONTENT_CHARS]}"},
    ]
    result = await provider.complete(prompt, max_tokens=500)
    generated = normalize_questions(
        parse_generated(result.text), exclude=[item.title, *(item.questions or [])], limit=8
    )
    if not generated:
        log.warning("questions_generation_empty", item_id=str(item.id))
        return []
    await update_item(db, item, {"questions_auto": generated, "change_note": "生成问法"}, actor)
    log.info("questions_generated", item_id=str(item.id), n=len(generated))
    return generated


async def add_question(db: AsyncSession, item: KnowledgeItem, question: str, actor=None) -> list[str]:
    """Append one customer phrasing (e.g. from the gap list) to the hand-written list."""
    merged = normalize_questions([*(item.questions or []), question], exclude=[item.title])
    if merged != list(item.questions or []):
        await update_item(db, item, {"questions": merged, "change_note": "补充问法"}, actor)
    return merged


async def items_missing_questions(db: AsyncSession, limit: int = _BULK_CAP) -> list[uuid.UUID]:
    """Published items with neither hand-written nor suggested questions."""
    rows = (
        await db.execute(
            select(KnowledgeItem.id)
            .where(
                KnowledgeItem.status == KnowledgeStatus.PUBLISHED,
                func.jsonb_array_length(KnowledgeItem.questions) == 0,
                func.jsonb_array_length(KnowledgeItem.questions_auto) == 0,
            )
            .order_by(KnowledgeItem.updated_at.desc())
            .limit(limit)
        )
    ).all()
    return [r[0] for r in rows]
