"""Classify what an assistant turn actually was, so "knowledge hit rate" means something.

`messages.citations` records what retrieval RETURNED, not what the answer used: the model
routinely gets a loosely related chunk, says "知识库里没有相关资料", and the turn still
counts as "grounded" on the dashboard. Greetings, handoff requests and guard refusals sit
in the same denominator. `classify` stamps every assistant message with one of:

    grounded   answer cites [来源N] (or confidently uses retrieved material) and does not
               say it found nothing
    no_info    retrieval ran but the answer admits the knowledge base has nothing
    general    no retrieval (or none that produced anything) — answered from general
               knowledge
    handoff    the turn escalated to a human, or the customer asked for one
    off_topic  guard refusal (decline_off_topic / jailbreak)
    chitchat   the customer message was a greeting / acknowledgement / too short to be
               a question
    degraded   provider error, truncation, empty-reply fallback

Effective hit rate = grounded / (grounded + no_info + general): business questions only.
`clean_citations` keeps the client-facing citation list honest: only the [来源N] refs the
answer actually mentions survive; an answer that says "没查到" keeps none.
"""

from __future__ import annotations

import re

KINDS = ("grounded", "no_info", "general", "handoff", "off_topic", "chitchat", "degraded")
BUSINESS_KINDS = ("grounded", "no_info", "general")  # the hit-rate denominator

# "The knowledge base has nothing on this" phrasings. Deliberately broad: a false
# positive turns one grounded answer into no_info, a false negative hides a gap.
NO_INFO_RE = re.compile(
    r"(没有查到|没查到|未查到|查不到|没有找到|未找到|找不到相关|没有明确|未明确|未说明|没有说明|"
    r"暂无相关|没有相关|没有提到|未提及|暂时没有(?:查到|找到|相关)|暂时查不到|无法确认|无法准确|"
    r"资料里没有|资料中没有|知识库(?:中|里)?(?:暂)?(?:没有|无|未)|没有这方面)"
)

# Greetings / acknowledgements / bare numbers: not questions, never a knowledge gap.
_CHITCHAT_RE = re.compile(
    r"^(?:你好|您好|在吗|在不在|在么|有人吗|hi|hello|hey|哈喽|嗨|谢谢|谢了|感谢|好的|好|行|嗯|哦|噢|"
    r"ok|okay|收到|明白|知道了|再见|拜拜|早上好|中午好|下午好|晚上好|早|晚安|测试|test|"
    r"[?？。.!！~～\s]+)+[?？!！。.~～\s]*$",
    re.I,
)
# A bare one- or two-digit number is a menu pick / "1" to wake the bot, not a question;
# "6.1" or "4o" is a model name and stays a question.
_BARE_NUMBER_RE = re.compile(r"^\d{1,2}[?？!！。.\s]*$")
_HUMAN_RE = re.compile(r"(人工|真人|客服|转接|找人|接线|工作人员)")
_CODE_TOKEN = re.compile(r"[A-Za-z0-9]")
_REF_RE = re.compile(r"\[来源\s*(\d+)\]")


def is_chitchat(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _CHITCHAT_RE.match(t) or _BARE_NUMBER_RE.match(t):
        return True
    # A single character ("好", "?") is not a question; two characters can be ("退款",
    # "发票"), and a short code (E1001, 6.1) always is.
    return len(t) <= 1 and not _CODE_TOKEN.search(t)


def wants_human(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 20 and bool(_HUMAN_RE.search(t))


def sounds_no_info(text: str) -> bool:
    return bool(NO_INFO_RE.search(text or ""))


def referenced_refs(text: str) -> set[int]:
    return {int(n) for n in _REF_RE.findall(text or "")}


def clean_citations(text: str, citations: list[dict]) -> list[dict]:
    """Citations the answer actually stands on.

    - refs mentioned as [来源N] → exactly those;
    - no refs and the answer says it found nothing → none;
    - no refs but a confident answer → keep all (the model used the material without
      numbering it; attribution is unknown, not absent)."""
    if not citations:
        return []
    refs = referenced_refs(text)
    if refs:
        return [c for c in citations if int(c.get("ref") or 0) in refs]
    if sounds_no_info(text):
        return []
    return list(citations)


def classify(
    *,
    user_text: str,
    final_text: str,
    citations: list[dict],
    searched: bool,
    escalated: bool = False,
    off_topic: bool = False,
    degraded: bool = False,
) -> str:
    """One of KINDS. `citations` is the CLEANED list (see clean_citations)."""
    if degraded:
        return "degraded"
    if off_topic:
        return "off_topic"
    if escalated:
        return "handoff"
    if is_chitchat(user_text):
        return "chitchat"
    if wants_human(user_text):
        return "handoff"
    if citations and not sounds_no_info(final_text):
        return "grounded"
    if searched:
        return "no_info"
    return "general"


def searched_knowledge(tool_trace: list[dict]) -> bool:
    return any(t.get("name") == "search_knowledge" for t in tool_trace or [])


def last_user_text(messages: list[dict]) -> str:
    """The current customer message from an assembled LLM message list (text parts only)."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        return ""
    return ""
