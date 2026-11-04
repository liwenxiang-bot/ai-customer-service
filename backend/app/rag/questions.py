"""Question variants ("同义问法") for FAQ-style knowledge.

Customers rarely phrase a question the way an answer is written ("显示限额了" vs
"提示额度限制，换号新开对话"), which is why a correct short FAQ item often reranks at
0.17–0.21 — barely above the floor. Each item can carry questions a customer might ask
(hand-written `questions`, LLM-suggested `questions_auto`); every question becomes its
own chunk, so retrieval matches question-to-question, and the hit injects the item's
body (see services.knowledge.reembed_item and rag.retrieval).

Question chunks share the knowledge_chunks table with body chunks and are told apart by
chunk_index: body chunks count from 0, question chunks from QUESTION_INDEX_BASE. No new
column, no schema change for the hot path, and span-merging (adjacent chunk_index) can
never fuse a question with a body chunk.
"""

from __future__ import annotations

import json
import re

QUESTION_INDEX_BASE = 10_000
MAX_QUESTIONS = 12
MAX_QUESTION_CHARS = 120

GENERATE_SYSTEM_PROMPT = (
    "你是客服知识库编辑。给定一条知识的标题和正文，写出客户可能用来问到这条知识的 6 到 8 种"
    "不同问法：口语化、简短（不超过 30 字）、角度各异（直接提问、描述报错或现象、描述场景、"
    "换用同义词、带错别字或缩写的常见写法）。不要重复标题，不要编造正文没有的信息，"
    "不要加编号。只返回一个 JSON 字符串数组，例如 [\"问法一\", \"问法二\"]。"
)


def is_question_index(chunk_index: int | None) -> bool:
    return chunk_index is not None and chunk_index >= QUESTION_INDEX_BASE


def normalize_questions(values, *, exclude: list[str] | None = None, limit: int = MAX_QUESTIONS) -> list[str]:
    """Trim, dedupe (case/space-insensitive), drop empties and anything in `exclude`."""
    seen = {_key(x) for x in (exclude or []) if x}
    out: list[str] = []
    for v in values or []:
        q = re.sub(r"\s+", " ", str(v or "")).strip().strip("“”\"'`")
        q = re.sub(r"^\d+[.、)]\s*", "", q)  # "1. 怎么退款" → "怎么退款"
        if not q or len(q) > MAX_QUESTION_CHARS:
            continue
        k = _key(q)
        if k in seen:
            continue
        seen.add(k)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def parse_generated(text: str) -> list[str]:
    """LLM output → list of questions. Tolerates code fences and a fallback to one
    question per line when the model ignores the JSON instruction."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            data = data.get("questions") or data.get("items") or []
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str | int | float)]
    except ValueError:
        pass
    lines = [re.sub(r"^[-*•]\s*", "", ln).strip() for ln in t.splitlines()]
    return [ln for ln in lines if ln and not ln.startswith(("[", "]", "{", "}"))]


def _key(q: str) -> str:
    return re.sub(r"[\s\W_]+", "", q or "").lower()
