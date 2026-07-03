"""Mine bad retrieval cases from live traffic (评测集回流).

The two strongest "retrieval didn't answer this well" signals already sit in the DB,
they just never flowed back into the eval set:

  - 👎 feedback on assistant messages — the retrieval trace (search_knowledge query,
    citation scores) is in Message.tool_calls / Message.citations;
  - handoff tickets the model raised itself (reason=model_decision) or that negative
    feedback raised (negative_feedback) — the session's last user question and the
    retrieval trace preceding the ticket tell what the KB failed to cover.

Output is two-fold:

  1. a scripts/rag_eval-compatible JSONL of *unlabelled* positives (expected_title is
     empty, so rag_eval skips them until a human fills in the item the query should
     have hit — or decides it's a coverage gap and adds knowledge instead);
  2. a human-readable summary on stdout, including down-voted/escalated turns WITHOUT
     any search_knowledge call — those can't be replayed as retrieval cases, but they
     are the coverage-gap triage queue.

Usage:  python -m scripts.mine_bad_cases [--days 30] [--limit 200] [--out mined_cases.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.models.conversation import HandoffTicket, Message
from app.models.enums import FeedbackKind, HandoffReason, MessageRole

log = get_logger("mine_bad_cases")


@dataclass
class BadCase:
    source: str  # 'feedback_down' | 'handoff:model_decision' | 'handoff:negative_feedback'
    session_id: str
    message_id: str | None
    user_question: str
    queries: list[str] = field(default_factory=list)  # search_knowledge queries, in order
    retrieved: list[dict] = field(default_factory=list)  # [{title, score}] from citations
    note: str = ""  # feedback note / ticket reason detail


def _search_queries(tool_calls: list) -> list[str]:
    out = []
    for tc in tool_calls or []:
        if isinstance(tc, dict) and tc.get("name") == "search_knowledge":
            q = ((tc.get("arguments") or {}).get("query") or "").strip()
            if q:
                out.append(q)
    return out


async def _prev_user_question(db, msg: Message) -> str:
    row = (
        await db.execute(
            select(Message.content)
            .where(
                Message.session_id == msg.session_id,
                Message.seq < msg.seq,
                Message.role == MessageRole.USER,
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row or ""


async def _mine_downvotes(db, cutoff: datetime, limit: int) -> list[BadCase]:
    msgs = (
        await db.execute(
            select(Message)
            .where(
                Message.role == MessageRole.ASSISTANT,
                Message.feedback == FeedbackKind.DOWN,
                Message.created_at >= cutoff,
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    cases = []
    for m in msgs:
        cases.append(BadCase(
            source="feedback_down",
            session_id=str(m.session_id),
            message_id=str(m.id),
            user_question=await _prev_user_question(db, m),
            queries=_search_queries(m.tool_calls),
            retrieved=[
                {"title": c.get("title", ""), "score": c.get("score")}
                for c in (m.citations or [])
            ],
            note=m.feedback_note or "",
        ))
    return cases


async def _mine_handoffs(db, cutoff: datetime, limit: int) -> list[BadCase]:
    tickets = (
        await db.execute(
            select(HandoffTicket)
            .where(
                HandoffTicket.reason.in_(
                    [HandoffReason.MODEL_DECISION, HandoffReason.NEGATIVE_FEEDBACK]
                ),
                HandoffTicket.created_at >= cutoff,
            )
            .order_by(HandoffTicket.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    cases = []
    for t in tickets:
        user_q = (
            await db.execute(
                select(Message.content)
                .where(
                    Message.session_id == t.session_id,
                    Message.role == MessageRole.USER,
                    Message.created_at <= t.created_at,
                )
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or ""
        # The retrieval trace lives on assistant messages; take the latest one before
        # the ticket that actually called search_knowledge.
        assistants = (
            await db.execute(
                select(Message)
                .where(
                    Message.session_id == t.session_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.created_at <= t.created_at,
                )
                .order_by(Message.seq.desc())
                .limit(5)
            )
        ).scalars().all()
        traced = next((m for m in assistants if _search_queries(m.tool_calls)), None)
        cases.append(BadCase(
            source=f"handoff:{t.reason}",
            session_id=str(t.session_id),
            message_id=str(traced.id) if traced else None,
            user_question=user_q,
            queries=_search_queries(traced.tool_calls) if traced else [],
            retrieved=[
                {"title": c.get("title", ""), "score": c.get("score")}
                for c in ((traced.citations if traced else None) or [])
            ],
            note=t.reason_detail or t.conversation_summary or "",
        ))
    return cases


def _write_jsonl(cases: list[BadCase], out: Path) -> int:
    """One unlabelled positive per distinct retrieval query — the labelling queue."""
    seen: set[str] = set()
    lines = []
    for case in cases:
        for q in case.queries:
            if q in seen:
                continue
            seen.add(q)
            lines.append(json.dumps({
                "type": "positive",
                "query": q,
                "expected_title": "",  # ← 人工标注：该查询应命中的条目标题（子串）
                "source": case.source,
                "session_id": case.session_id,
                "message_id": case.message_id,
                "user_question": case.user_question,
                "retrieved": case.retrieved,
                "note": case.note,
            }, ensure_ascii=False))
    header = (
        "# 由 scripts/mine_bad_cases 生成的坏例队列。人工分流：\n"
        "#  - 漏召回 → 填 expected_title 后并入评测集（rag_eval --cases）\n"
        "#  - 覆盖缺口 → 不填，转去补知识库\n"
    )
    out.write_text(header + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def _print_summary(cases: list[BadCase]) -> None:
    with_trace = [c for c in cases if c.queries]
    no_trace = [c for c in cases if not c.queries]
    print(f"\n=== 坏例 {len(cases)} 条（含检索痕迹 {len(with_trace)}，无检索 {len(no_trace)}）===")
    for c in with_trace:
        print(f"\n[{c.source}] session={c.session_id}")
        if c.user_question:
            print(f"  用户问题: {c.user_question[:120]}")
        for q in c.queries:
            print(f"  检索 query: {q}")
        for r in c.retrieved[:5]:
            print(f"    命中: {r['title']}  score={r['score']}")
        if c.note:
            print(f"  备注: {c.note[:120]}")
    if no_trace:
        print("\n--- 无 search_knowledge 调用（不可回放为检索用例；覆盖缺口/闲聊分流队列）---")
        for c in no_trace:
            print(f"[{c.source}] session={c.session_id}  问题: {c.user_question[:100]!r}  备注: {c.note[:80]!r}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mine bad retrieval cases from live traffic")
    parser.add_argument("--days", type=int, default=30, help="回看天数（默认 30）")
    parser.add_argument("--limit", type=int, default=200, help="每类信号最多取多少条（默认 200）")
    parser.add_argument("--out", default="mined_cases.jsonl", help="输出 JSONL 路径")
    parser.add_argument(
        "--tenant", default=str(DEFAULT_TENANT_ID),
        help="租户 id（RLS 上下文；单租户部署用默认值即可）",
    )
    return parser.parse_args()


async def main() -> None:
    configure_logging()
    args = _parse_args()
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    with tenant_scope(args.tenant):
        async with session_scope() as db:
            down = await _mine_downvotes(db, cutoff, args.limit)
            handoff = await _mine_handoffs(db, cutoff, args.limit)
    # A 👎 that also raised a negative_feedback ticket would appear twice; keep the
    # message-level record (richer trace) and drop the ticket duplicate.
    seen_msgs = {c.message_id for c in down if c.message_id}
    cases = down + [c for c in handoff if c.message_id not in seen_msgs]

    _print_summary(cases)
    written = _write_jsonl(cases, Path(args.out))
    print(f"\nwrote {written} unlabelled case(s) → {args.out}")
    log.info("bad_cases_mined", total=len(cases), jsonl=written, days=args.days)


if __name__ == "__main__":
    asyncio.run(main())
