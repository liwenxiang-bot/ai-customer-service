"""Threshold calibration from labelled questions — "一键校准" behind the admin UI.

The two gates that decide whether a loosely related chunk counts as a hit are
`min_score` (rerank score floor, when rerank is on) and `vector_min_sim` (cosine floor
otherwise). Picking them by eye does not work (see docs/RAG-PLAN-2026-11-04.md §2.2), so:

1. operators label real questions (`retrieval_eval_cases`): the item that should come
   back, or "nothing" for questions the knowledge base cannot answer;
2. `run_calibration` retrieves every case once with the gate relaxed, keeps the
   candidate scores, and evaluates each candidate threshold offline (rerank mode) —
   or re-runs retrieval per candidate floor (vector mode, no rerank);
3. the report lists hit rate (positives found in top_k) and rejection rate (negatives
   returned empty) per threshold, plus a recommendation: highest accuracy, ties broken
   toward the value closest to the current setting.

Nothing is applied automatically; the UI saves the recommended value through the
normal config update.
"""

from __future__ import annotations

import types
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.config import AIConfig
from app.models.knowledge import RetrievalEvalCase
from app.rag.retrieval import hybrid_search
from app.services.ai_config import merged_retrieval, to_rerank_settings

log = get_logger("retrieval_eval")

RERANK_SWEEP = [round(0.05 * i, 2) for i in range(1, 11)]  # 0.05 … 0.50
VECTOR_SWEEP = [round(0.40 + 0.05 * i, 2) for i in range(0, 7)]  # 0.40 … 0.70
_RELAXED_VECTOR_FLOOR = 0.30
MAX_CASES = 300


@dataclass
class CaseRun:
    query: str
    expected_item_id: str | None  # None ⇒ should return nothing
    expected_title: str = ""
    # (item_id, title, score) of what came back with the gate relaxed, best first
    candidates: list[tuple[str, str, float]] = field(default_factory=list)
    score_kind: str = "fusion"


def _proxy(cfg: AIConfig, **retrieval_overrides) -> types.SimpleNamespace:
    """A config stand-in with a modified retrieval dict (hybrid_search only reads attributes)."""
    ns = types.SimpleNamespace()
    for k in (
        "tenant_id", "rerank_enabled", "rerank_base_url", "rerank_api_key_enc", "rerank_model",
        "embedding_base_url", "embedding_api_key_enc", "embedding_model", "embedding_dim",
    ):
        setattr(ns, k, getattr(cfg, k, None))
    ns.retrieval = {**merged_retrieval(cfg), **retrieval_overrides}
    return ns


def evaluate(runs: list[CaseRun], threshold: float, top_k: int) -> dict:
    """Offline evaluation of one threshold over captured candidate scores."""
    pos = [r for r in runs if r.expected_item_id]
    neg = [r for r in runs if not r.expected_item_id]
    hits = 0
    for r in pos:
        kept = [c for c in r.candidates if c[2] >= threshold][:top_k]
        if any(c[0] == r.expected_item_id for c in kept):
            hits += 1
    rejects = sum(1 for r in neg if not any(c[2] >= threshold for c in r.candidates))
    total = len(pos) + len(neg)
    return {
        "value": threshold,
        "hits": hits, "positives": len(pos),
        "rejects": rejects, "negatives": len(neg),
        "hit_rate": round(hits / len(pos), 3) if pos else None,
        "reject_rate": round(rejects / len(neg), 3) if neg else None,
        "accuracy": round((hits + rejects) / total, 3) if total else None,
    }


def recommend(rows: list[dict], current: float) -> float | None:
    """Highest accuracy; ties go to the value nearest the current setting."""
    scored = [r for r in rows if r.get("accuracy") is not None]
    if not scored:
        return None
    best = max(r["accuracy"] for r in scored)
    tied = [r["value"] for r in scored if r["accuracy"] == best]
    return min(tied, key=lambda v: (abs(v - current), v))


async def list_cases(db: AsyncSession) -> list[RetrievalEvalCase]:
    return list(
        (await db.execute(select(RetrievalEvalCase).order_by(RetrievalEvalCase.created_at.desc()))).scalars()
    )


async def add_case(
    db: AsyncSession, query: str, expected_item_id: str | None, expected_title: str = "",
    source: str = "manual", actor_email: str = "",
) -> RetrievalEvalCase:
    row = RetrievalEvalCase(
        query=query.strip()[:2000],
        expected_item_id=uuid.UUID(expected_item_id) if expected_item_id else None,
        expected_title=(expected_title or "").strip()[:500],
        source=source[:32],
        created_by=actor_email or "",
    )
    db.add(row)
    await db.flush()
    return row


async def run_calibration(db: AsyncSession, cfg: AIConfig) -> dict:
    cases = (await list_cases(db))[:MAX_CASES]
    params = merged_retrieval(cfg)
    top_k = int(params["top_k"])
    rer = to_rerank_settings(cfg)
    rerank_mode = bool(rer and rer.api_key)
    current = float(params["min_score"] if rerank_mode else params["vector_min_sim"])
    if not cases:
        return {
            "cases": 0, "positives": 0, "negatives": 0,
            "mode": "rerank" if rerank_mode else "vector", "param": "min_score" if rerank_mode else "vector_min_sim",
            "current": current, "sweep": [], "recommended": None, "per_case": [],
        }

    runs: list[CaseRun] = []
    if rerank_mode:
        relaxed = _proxy(cfg, min_score=0.0, vector_min_sim=_RELAXED_VECTOR_FLOOR)
        for c in cases:
            run = CaseRun(c.query, str(c.expected_item_id) if c.expected_item_id else None, c.expected_title)
            try:
                results = await hybrid_search(db, relaxed, c.query)
            except Exception as exc:  # noqa: BLE001 — one bad case must not kill the sweep
                log.warning("calibration_case_failed", query=c.query[:60], error=str(exc))
                results = []
            run.candidates = [(r.item_id, r.title, float(r.score)) for r in results]
            run.score_kind = results[0].score_kind if results else "rerank"
            runs.append(run)
        if any(r.score_kind != "rerank" for r in runs if r.candidates):
            # Rerank degraded during the sweep: fusion scores cannot be thresholded.
            return {"error": "重排服务本次不可用，无法校准重排分下限，请稍后重试", "cases": len(cases)}
        sweep = [evaluate(runs, t, top_k) for t in RERANK_SWEEP]
    else:
        # No rerank: the floor is applied inside retrieval, so re-run per candidate value.
        per_value: dict[float, list[CaseRun]] = {}
        for v in VECTOR_SWEEP:
            proxy = _proxy(cfg, vector_min_sim=v, min_score=0.0)
            rows: list[CaseRun] = []
            for c in cases:
                run = CaseRun(c.query, str(c.expected_item_id) if c.expected_item_id else None, c.expected_title)
                try:
                    results = await hybrid_search(db, proxy, c.query)
                except Exception as exc:  # noqa: BLE001
                    log.warning("calibration_case_failed", query=c.query[:60], error=str(exc))
                    results = []
                # Scores are fusion ranks here; mark every returned candidate as "kept".
                run.candidates = [(r.item_id, r.title, 1.0) for r in results]
                rows.append(run)
            per_value[v] = rows
        sweep = [{**evaluate(per_value[v], 0.0, top_k), "value": v} for v in VECTOR_SWEEP]
        runs = per_value.get(min(VECTOR_SWEEP, key=lambda v: abs(v - current)), [])

    recommended = recommend(sweep, current)
    now = evaluate(runs, current if rerank_mode else 0.0, top_k) if rerank_mode else next(
        (s for s in sweep if abs(s["value"] - current) < 1e-9), None
    )
    per_case = []
    for r in runs:
        kept = [c for c in r.candidates if c[2] >= (current if rerank_mode else 0.0)][:top_k]
        found = any(c[0] == r.expected_item_id for c in kept) if r.expected_item_id else not kept
        per_case.append({
            "query": r.query,
            "expected_title": r.expected_title or ("（应无结果）" if not r.expected_item_id else ""),
            "ok_at_current": found,
            "top_title": r.candidates[0][1] if r.candidates else None,
            "top_score": round(r.candidates[0][2], 4) if r.candidates else None,
        })
    return {
        "cases": len(cases),
        "positives": sum(1 for c in cases if c.expected_item_id),
        "negatives": sum(1 for c in cases if not c.expected_item_id),
        "mode": "rerank" if rerank_mode else "vector",
        "param": "min_score" if rerank_mode else "vector_min_sim",
        "current": current,
        "current_eval": now,
        "sweep": sweep,
        "recommended": recommended,
        "per_case": per_case,
    }
