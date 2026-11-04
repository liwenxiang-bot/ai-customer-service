"""Knowledge gaps: the customer questions the knowledge base could not answer.

Three signals already sit in `messages`; this module turns them into a worklist:

  miss        a search_knowledge call that returned nothing (tool result text)
  no_info     an answer classified no_info — retrieval ran, the model said it found nothing
  unsearched  an answer classified general — a business question answered without the
              knowledge base at all

Rows are grouped by meaning, not spelling: questions whose jieba token sets overlap
(Jaccard ≥ 0.5) or whose normalised text is identical form one group, so "GPT-6.1
没了" and "为什么现在没有 gpt6.1" count once. Dismissed questions
(knowledge_gap_ignores) hide their whole group, rephrasings included.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import app_day_start_utc, app_today
from app.models.knowledge import KnowledgeGapIgnore
from app.rag.segment import segment_query
from app.services.answer_kind import is_chitchat, wants_human

_MAX_ROWS = 5000
_MAX_GROUPS = 200
_JACCARD = 0.5
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)

_MISS_SQL = """
WITH a AS (
    SELECT m.id, m.session_id, m.seq, m.created_at, m.answer_kind, m.tool_calls
    FROM messages m
    WHERE m.role = 'assistant' AND m.model <> 'human' AND m.created_at >= :start
    ORDER BY m.created_at DESC
    LIMIT :limit
)
SELECT
    (SELECT u.content FROM messages u
      WHERE u.session_id = a.session_id AND u.role = 'user' AND u.seq < a.seq
      ORDER BY u.seq DESC LIMIT 1) AS question,
    a.session_id::text, a.created_at, a.answer_kind,
    EXISTS (SELECT 1 FROM jsonb_array_elements(a.tool_calls) t
             WHERE t->>'name' = 'search_knowledge'
               AND (t->>'result' LIKE '%未在知识库中命中%' OR t->>'result' LIKE '%仍未命中%')) AS missed,
    EXISTS (SELECT 1 FROM jsonb_array_elements(a.tool_calls) t
             WHERE t->>'name' = 'search_knowledge') AS searched
FROM a
"""


def normalize(q: str) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFKC", q or "")).lower()


def tokens(q: str) -> frozenset[str]:
    seg = segment_query(q or "")
    toks = {t.lower() for t in seg.split(" or ") if t} if seg else set()
    return frozenset(toks or {normalize(q)})


def similar(a_key: str, a_toks: frozenset, b_key: str, b_toks: frozenset) -> bool:
    if a_key and a_key == b_key:
        return True
    if not a_toks or not b_toks:
        return False
    inter = len(a_toks & b_toks)
    return inter / len(a_toks | b_toks) >= _JACCARD


def gap_kind(answer_kind: str, missed: bool, searched: bool) -> str | None:
    """Which worklist bucket a row belongs to, or None when it is not a gap."""
    if answer_kind == "no_info":
        return "no_info"
    if missed:
        return "miss"
    if answer_kind == "general":
        return "unsearched"
    if answer_kind == "" and not searched:
        return None  # legacy row, unknown — don't guess
    return None


def group_questions(rows: list[dict], ignored: list[str] | None = None) -> list[dict]:
    """rows: [{question, session_id, created_at, kind}] newest first → grouped worklist."""
    ignored_sets = [(normalize(q), tokens(q)) for q in (ignored or []) if q and q.strip()]
    groups: list[dict] = []
    for r in rows:
        q = (r.get("question") or "").strip()
        if not q or is_chitchat(q) or wants_human(q):
            continue
        key, toks = normalize(q), tokens(q)
        if any(similar(key, toks, ik, it) for ik, it in ignored_sets):
            continue
        for g in groups:
            if similar(key, toks, g["_key"], g["_toks"]):
                g["count"] += 1
                g["kinds"][r["kind"]] = g["kinds"].get(r["kind"], 0) + 1
                if q not in g["examples"] and len(g["examples"]) < 3:
                    g["examples"].append(q)
                break
        else:
            groups.append({
                "_key": key, "_toks": toks,
                "question": q[:200], "count": 1,
                "kinds": {r["kind"]: 1},
                "last_at": r.get("created_at"),
                "session_id": r.get("session_id"),
                "examples": [q],
            })
    groups.sort(key=lambda g: (-g["count"], str(g["last_at"] or "")), reverse=False)
    groups.sort(key=lambda g: g["count"], reverse=True)
    out = []
    for g in groups[:_MAX_GROUPS]:
        out.append({
            "question": g["question"], "count": g["count"], "kinds": g["kinds"],
            "last_at": g["last_at"].isoformat() if hasattr(g["last_at"], "isoformat") else g["last_at"],
            "session_id": g["session_id"], "examples": g["examples"],
        })
    return out


async def collect_gaps(db: AsyncSession, days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    start = app_day_start_utc(app_today() - timedelta(days=days - 1))
    rows = (await db.execute(text(_MISS_SQL), {"start": start, "limit": _MAX_ROWS})).all()
    items = []
    for question, session_id, created_at, answer_kind, missed, searched in rows:
        kind = gap_kind(answer_kind or "", bool(missed), bool(searched))
        if kind:
            items.append({"question": question, "session_id": session_id,
                          "created_at": created_at, "kind": kind})
    ignored = [q for (q,) in (await db.execute(select(KnowledgeGapIgnore.question))).all()]
    groups = group_questions(items, ignored)
    return {
        "days": days,
        "scanned_answers": len(rows),
        "gap_answers": len(items),
        "groups": groups,
        "ignored": len(ignored),
    }


async def ignore_question(db: AsyncSession, question: str, actor_email: str = "") -> KnowledgeGapIgnore:
    row = KnowledgeGapIgnore(question=question.strip()[:500], created_by=actor_email or "")
    db.add(row)
    await db.flush()
    return row
