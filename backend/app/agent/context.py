"""Conversation context assembly + long-context management.

Builds the message list sent to the LLM from: system prompt → rolling summary of old
turns → recent turns that fit a token budget. Older turns beyond the budget are
compressed into `session.summary` (the actual summarization runs as a background task;
see tasks/summarize). This caps cost and avoids blowing the model window
(requirements §6.1).
"""

from __future__ import annotations

import base64

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
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


def _is_image(att: dict) -> bool:
    return att.get("kind") == "image" or str(att.get("content_type", "")).startswith("image/")


def messages_have_image_parts(messages: list[Message]) -> bool:
    """Return True when the assembled prompt contains OpenAI-style image parts."""
    return any(_content_has_image_parts(m.get("content")) for m in messages)


def _content_has_image_parts(content: object) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict)
        and (part.get("type") == "image_url" or bool(part.get("image_url")))
        for part in content
    )


async def _image_payload_url(a: dict) -> str | None:
    """Inline the image as a base64 data URL (works regardless of whether the LLM/relay
    can fetch our media URL); fall back to the public URL if the object can't be read."""
    key = a.get("key")
    if key:
        try:
            data, ct = await storage.fetch_object(key)
            return f"data:{ct};base64,{base64.b64encode(data).decode()}"
        except Exception:  # noqa: BLE001 — fall back to the public URL
            log.warning("image_inline_failed", key=key)
    return a.get("url")


async def _content_for(m: DBMessage, *, vision: bool, latest: bool) -> str | list:
    """Render a message's content. Images are sent as multimodal parts only for the
    current turn when vision is on; otherwise attachments degrade to a text note."""
    atts = m.attachments or []
    if not atts:
        return m.content
    names = "、".join(a.get("name") or "附件" for a in atts)
    if vision and latest and any(_is_image(a) for a in atts):
        parts: list = []
        if m.content:
            parts.append({"type": "text", "text": m.content})
        for a in atts:
            if _is_image(a):
                url = await _image_payload_url(a)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
        files = [a.get("name") or "附件" for a in atts if not _is_image(a)]
        if files:
            parts.append({"type": "text", "text": f"（另附文件：{'、'.join(files)}）"})
        return parts or [{"type": "text", "text": "（图片）"}]
    note = f"（用户上传了 {len(atts)} 个附件：{names}）"
    return f"{m.content}\n{note}".strip() if m.content else note


async def build_messages(
    db: AsyncSession,
    session: Session,
    ai_config: AIConfig,
    channel_system_prompt: str | None = None,
    image_understanding: bool = False,
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
        if not m.content and not m.attachments:
            continue
        cost = _count_tokens(m.content) + 4
        if budget - cost < 0 and selected:
            break
        budget -= cost
        selected.append(m)

    latest_user_seq = max((m.seq for m in selected if m.role == MessageRole.USER), default=-1)
    for m in reversed(selected):
        messages.append(
            {
                "role": m.role,
                "content": await _content_for(m, vision=image_understanding, latest=m.seq == latest_user_seq),
            }
        )

    return messages


def needs_summarization(session: Session, threshold_messages: int = 24) -> bool:
    """Heuristic: once unsummarized turns pile up, schedule a compression pass."""
    return (session.message_count - session.summarized_until_seq) >= threshold_messages
