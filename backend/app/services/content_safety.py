"""Content safety filtering (input/output). Off by default; toggled per AIConfig.

Ships with a lightweight local sensitive-word screen so the seam exists and works out
of the box; a real moderation API can be plugged in here later (requirements §11).
Treats both user input and knowledge as DATA — it never executes embedded instructions.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.models.config import AIConfig

log = get_logger("content_safety")

# Minimal placeholder list; real deployments load a maintained lexicon or call an API.
_BLOCKLIST: set[str] = set()

_INPUT_NOTICE = "抱歉，你的消息包含暂不支持的内容，请调整后再发送。"
_OUTPUT_FALLBACK = "抱歉，这个问题我暂时不方便回答，建议你咨询人工客服。"


def _hit(text: str) -> bool:
    if not _BLOCKLIST:
        return False
    low = text.lower()
    return any(word in low for word in _BLOCKLIST)


async def check_input(ai_config: AIConfig, text: str) -> tuple[bool, str]:
    """Return (is_safe, notice_if_unsafe)."""
    if not ai_config.content_safety_enabled:
        return True, ""
    if _hit(text):
        log.info("input_blocked")
        return False, _INPUT_NOTICE
    return True, ""


async def check_output(ai_config: AIConfig, text: str) -> tuple[bool, str]:
    if not ai_config.content_safety_enabled:
        return True, text
    if _hit(text):
        log.info("output_filtered")
        return False, _OUTPUT_FALLBACK
    return True, text
