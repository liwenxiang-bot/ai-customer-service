"""Conversation context assembly + long-context management.

Builds the message list sent to the LLM from: system prompt → rolling summary of old
turns → recent turns that fit a token budget. Older turns beyond the budget are
compressed into `session.summary` (the actual summarization runs as a background task;
see tasks/summarize). This caps cost and avoids blowing the model window
(requirements §6.1).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.llm.types import Message
from app.models.config import AIConfig
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import MessageRole

log = get_logger("agent.context")

# Leave headroom for the model's reply + tool schemas.
_HISTORY_TOKEN_BUDGET = 6000

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        return len(_enc.encode(text))
except Exception:  # pragma: no cover - tiktoken optional at runtime

    def _count_tokens(text: str) -> int:
        # Heuristic fallback: CJK ~1 token/char, latin ~1 token/4 chars.
        cjk = sum(1 for c in text if "一" <= c <= "鿿")
        return cjk + (len(text) - cjk) // 4 + 1


def estimate_tokens(text: str) -> int:
    return _count_tokens(text)


def _system_prompt(ai_config: AIConfig, channel_override: str | None) -> str:
    base = (channel_override or ai_config.system_prompt or "").strip()
    return base


async def build_messages(
    db: AsyncSession,
    session: Session,
    ai_config: AIConfig,
    channel_system_prompt: str | None = None,
) -> list[Message]:
    """Assemble the LLM message list for the current turn (history already persisted)."""
    messages: list[Message] = []

    sys = _system_prompt(ai_config, channel_system_prompt)
    if sys:
        messages.append({"role": "system", "content": sys})

    if session.summary:
        messages.append(
            {
                "role": "system",
                "content": f"【对话历史摘要】\n{session.summary}",
            }
        )

    # Recent turns after the summarized watermark, newest-first within budget.
    rows = (
        await db.execute(
            select(DBMessage)
            .where(
                DBMessage.session_id == session.id,
                DBMessage.seq > session.summarized_until_seq,
                DBMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
            )
            .order_by(DBMessage.seq.desc())
        )
    ).scalars().all()

    selected: list[DBMessage] = []
    budget = _HISTORY_TOKEN_BUDGET
    for m in rows:
        if not m.content:
            continue
        cost = _count_tokens(m.content) + 4
        if budget - cost < 0 and selected:
            break
        budget -= cost
        selected.append(m)

    for m in reversed(selected):
        messages.append({"role": m.role, "content": m.content})

    return messages


def needs_summarization(session: Session, threshold_messages: int = 24) -> bool:
    """Heuristic: once unsummarized turns pile up, schedule a compression pass."""
    return (session.message_count - session.summarized_until_seq) >= threshold_messages
