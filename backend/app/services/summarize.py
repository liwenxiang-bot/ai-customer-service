"""Long-context compression: fold older turns into session.summary (requirements §6.1)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.llm.factory import get_provider
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole
from app.services.ai_config import get_active_ai_config, to_llm_settings

log = get_logger("summarize")

_KEEP_RECENT = 8  # leave the most recent turns verbatim

# The summary is the only antecedent source for query rewriting once old turns are
# folded (context.py injects it above recent messages): if entities get paraphrased
# away, later self-contained retrieval queries lose their subject and miss the KB.
# The trailing entity line keeps them machine-spottable across rolling re-summaries.
_SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。请把以下客服对话压缩成简洁的中文要点，"
    "保留用户身份、关键诉求、已确认的信息和待办，省略寒暄。\n"
    "硬性要求：对话和已有摘要中出现过的产品名称、型号、错误码、订单号、金额、"
    "时间等具体实体必须按原文一字不差保留，禁止泛化成「某产品」「某个错误」，"
    "用户仍在讨论的对象尤其不能丢。摘要末尾固定加一行「涉及实体：」，"
    "用逗号分隔列出上述全部实体原文。"
)


async def summarize_session(db: AsyncSession, session_id: str) -> bool:
    session = await db.get(Session, uuid.UUID(session_id))
    if not session:
        return False

    rows = (
        await db.execute(
            select(DBMessage)
            .where(
                DBMessage.session_id == session.id,
                DBMessage.seq > session.summarized_until_seq,
                DBMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
            )
            .order_by(DBMessage.seq.asc())
        )
    ).scalars().all()

    if len(rows) <= _KEEP_RECENT:
        return False

    to_summarize = rows[:-_KEEP_RECENT]
    watermark = to_summarize[-1].seq
    transcript = "\n".join(
        f"{'用户' if m.role == MessageRole.USER else '客服'}：{m.content}" for m in to_summarize
    )

    ai_config = await get_active_ai_config(db)
    provider = get_provider(to_llm_settings(ai_config))
    prompt = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": f"已有摘要：\n{session.summary or '（无）'}\n\n新对话：\n{transcript}\n\n请输出更新后的完整摘要："},
    ]
    try:
        # 500 (not 400) leaves headroom for the mandatory entity list line.
        result = await provider.complete(prompt, max_tokens=500)
        if result.text:
            session.summary = result.text.strip()
            session.summarized_until_seq = watermark
            await db.commit()
            log.info("session_summarized", session_id=session_id, watermark=watermark)
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning("summarize_failed", error=str(exc))
    return False
