"""Hybrid retrieval: vector (semantic) + keyword (exact) → fusion → optional rerank.

This is the core of answer quality. Pure vector search misses exact terms (product
models, error codes); pure keyword misses paraphrases. We run both and fuse with
weighted Reciprocal Rank Fusion (scale-free, robust), then optionally rerank.

Relevance gating (so we don't "always hit" the nearest-but-irrelevant chunk):
  - vector path drops chunks below `vector_min_sim` (cosine similarity) BEFORE fusion;
  - when rerank succeeds, results below `min_score` (rerank score) are dropped after it;
  - when rerank is off or degrades at runtime, vector-scored rows are held to
    `vector_min_sim` explicitly, and keyword-only rows — which cleared only a boolean
    tsv/trgm match — are re-checked against the query vector with the looser
    `keyword_min_sim` floor (exact code-token hits exempt: scoring low on cosine while
    being the right answer is precisely why the keyword path exists).
A query with nothing relevant returns [] → the tool tells the model to escalate
instead of answering from weak context.

Chinese keyword search: `tsv` indexes a jieba-segmented copy of the content (see
rag.segment); the query is segmented the same way (NFKC-folded, stopwords dropped).
The exact-term branch matches `query <% content` (word_similarity: the query's
trigrams against the best window of the chunk, GIN-indexed) — the old symmetric
similarity() could never clear any threshold for a short code against a 600-char
chunk. Short alphanumeric queries also get an ILIKE substring branch, which is what
catches separator variants (SKU-12 vs SKU12).

Post-fusion shaping (before top_k): same-item hits whose chunk_index ranges adjoin or
overlap merge into one result — expanded once over the merged span instead of
injecting 2/3-identical context blocks — and a soft per-item quota stops one long
document from monopolising top_k while other items still have candidates.

Freshness: both paths exclude items outside their effective_from/expires_at window.

Every stage degrades gracefully: if embeddings are unavailable we fall back to
keyword-only; if the whole thing fails the caller falls back to a pure-LLM answer.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import retrieval_calls, retrieval_top_score
from app.llm.embeddings import EmbeddingSettings
from app.llm.factory import get_embedding_client, get_rerank_client
from app.models.config import AIConfig
from app.rag.questions import QUESTION_INDEX_BASE, is_question_index
from app.rag.segment import segment_query
from app.services.ai_config import (
    merged_retrieval,
    to_embedding_settings,
    to_rerank_settings,
)

log = get_logger("rag.retrieval")

_RRF_K = 60  # RRF damping constant

# Tokens carrying ASCII alphanumerics (error codes, SKUs, model numbers) — the
# exact-term queries the keyword path exists for.
_CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./]*")

# Same-item hits at most this many chunk_index apart count as one passage: with
# chunk_overlap they share text, and their expanded contexts are ~2/3 identical.
_MERGE_GAP = 2


@dataclass
class RetrievalResult:
    item_id: str
    chunk_id: str
    title: str
    content: str  # the matched chunk — citation-accurate
    score: float
    context: str = ""  # matched chunk expanded with neighbours (fed to the LLM)
    score_kind: str = "fusion"  # what `score` means: 'fusion' (weighted RRF) or 'rerank'
    matched_question: str = ""  # set when a customer-phrasing chunk (rag.questions) matched


@dataclass
class _Group:
    """A run of same-item hits whose chunk_index ranges adjoin/overlap.

    `rep` is the best-scoring member (fused row layout); lo/hi span the members'
    chunk_index range. lo/hi are None when the index is unknown (lookup degraded) —
    such rows never merge and fall back to un-expanded content."""

    rep: tuple
    lo: int | None
    hi: int | None


async def _embed_query(emb_cfg: EmbeddingSettings, query: str) -> list[float] | None:
    """Query vector, or None when embeddings are unconfigured or down.

    None switches the whole pipeline to keyword-only mode: no vector path, and no
    vector recheck of keyword survivors (graceful degradation over strictness)."""
    if not emb_cfg.api_key:
        return None
    try:
        qvec = await get_embedding_client(emb_cfg).embed_one(query, text_type="query")
        return qvec or None
    except Exception as exc:  # noqa: BLE001 — degrade, never break the turn
        log.warning("query_embed_degraded", error=str(exc))
        return None


def _vec_literal(qvec: list[float]) -> str:
    return "[" + ",".join(str(x) for x in qvec) + "]"


async def _vector_search(
    db: AsyncSession, emb_cfg: EmbeddingSettings, qvec: list[float], limit: int, min_sim: float
) -> tuple[list[tuple[str, str, str, str, float]], float | None]:
    """([(item_id, chunk_id, title, content, similarity)], raw top-1 similarity).

    Hits are gated by min_sim; the top-1 similarity is reported BEFORE the gate — the
    part of the distribution the gate removes is exactly what calibrating
    `vector_min_sim` needs to see. Returns ([], None) on any failure (degrade to
    keyword-only)."""
    try:
        # HNSW recall tuning (applies to this transaction only):
        #  - ef_search defaults to 40 in pgvector; with LIMIT == candidate pool we'd be at the
        #    recall edge, so widen the search list to ~2× the pool.
        #  - iterative_scan (pgvector 0.8+) keeps probing the index until enough rows survive
        #    the status/published/dim WHERE filter — without it a heavily-filtered query can
        #    silently under-return. Guarded by a savepoint so older pgvector just skips it.
        ef = min(max(limit * 2, 100), 1000)
        await db.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
        try:
            async with db.begin_nested():
                await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        except Exception:  # noqa: BLE001 — pgvector <0.8 lacks this GUC; ef_search alone still helps
            pass
        # ORDER BY <=> LIMIT uses the HNSW index; we return similarity and apply the
        # floor in Python so the index scan stays fast.
        # embedding_model filter: same-dim model swaps must never cosine-compare across
        # vector spaces (the scores would be noise); '' keeps legacy rows embedded
        # before the column existed retrievable until they're re-embedded.
        sql = text(
            """
            SELECT c.item_id::text, c.id::text, i.title, c.content,
                   1 - (c.embedding <=> CAST(:qvec AS vector)) AS sim
            FROM knowledge_chunks c
            JOIN knowledge_items i ON i.id = c.item_id
            WHERE c.embedding IS NOT NULL
              AND c.status = 'ready'
              AND i.status = 'published'
              AND (i.effective_from IS NULL OR i.effective_from <= now())
              AND (i.expires_at IS NULL OR i.expires_at > now())
              AND c.embedding_dim = :dim
              AND (c.embedding_model = :model OR c.embedding_model = '')
            ORDER BY c.embedding <=> CAST(:qvec AS vector)
            LIMIT :limit
            """
        )
        rows = (
            await db.execute(
                sql,
                {
                    "qvec": _vec_literal(qvec),
                    "limit": limit,
                    "dim": emb_cfg.dim,
                    "model": emb_cfg.model,
                },
            )
        ).all()
        top_sim = float(rows[0][4]) if rows and rows[0][4] is not None else None
        hits = [
            (r[0], r[1], r[2], r[3], float(r[4]))
            for r in rows
            if r[4] is not None and float(r[4]) >= min_sim
        ]
        return hits, top_sim
    except Exception as exc:  # noqa: BLE001 — degrade, never break the turn
        log.warning("vector_search_degraded", error=str(exc))
        return [], None


async def _keyword_search(
    db: AsyncSession, query: str, limit: int, trgm_threshold: float
) -> list[tuple[str, str, str, str]]:
    """Full-text (jieba-segmented tsvector, OR-matched) + word-similarity exact-term matching."""
    try:
        q = query.strip()
        q_or = segment_query(q) or q  # OR over tokens (see segment_query)
        # `<%`'s threshold lives in a GUC; set_config(..., is_local=true) scopes it to
        # this transaction. The operator form (not `word_similarity(...) > x`) is what
        # lets the planner use the ix_chunks_content_trgm GIN index.
        await db.execute(
            text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)"),
            {"t": str(trgm_threshold)},
        )
        params: dict = {"q_or": q_or, "q": q, "limit": limit}
        # Short code-ish queries also get an exact substring branch: word_similarity
        # can't bridge separator variants (SKU-12 vs SKU12 ≈ 0.17), ILIKE can — and
        # gin_trgm_ops accelerates ILIKE too.
        ilike = ""
        if len(q) <= 20 and _CODE_TOKEN.search(q):
            ilike = "OR c.content ILIKE :q_like"
            params["q_like"] = f"%{q}%"
        sql = text(
            f"""
            SELECT c.item_id::text, c.id::text, i.title, c.content
            FROM knowledge_chunks c
            JOIN knowledge_items i ON i.id = c.item_id
            WHERE i.status = 'published'
              AND (i.effective_from IS NULL OR i.effective_from <= now())
              AND (i.expires_at IS NULL OR i.expires_at > now())
              AND (c.tsv @@ websearch_to_tsquery('simple', :q_or)
                   OR :q <% c.content
                   {ilike})
            ORDER BY (ts_rank_cd(c.tsv, websearch_to_tsquery('simple', :q_or)) * 2
                      + word_similarity(:q, c.content)) DESC
            LIMIT :limit
            """
        )
        rows = (await db.execute(sql, params)).all()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("keyword_search_degraded", error=str(exc))
        return []


def _fuse(
    vector_hits: list[tuple], keyword_hits: list[tuple], vw: float, kw: float
) -> list[tuple[str, str, str, str, float, float | None]]:
    """Weighted RRF over the two ranked lists (keyed by chunk_id).

    Output rows are (item_id, chunk_id, title, content, rrf_score, sim) where `sim` is
    the vector cosine similarity if the chunk was a vector hit, else None. Accepts both
    4-tuples and 5-tuples (with similarity) as input."""
    scores: dict[str, float] = {}
    payload: dict[str, tuple] = {}
    sims: dict[str, float] = {}
    for rank, hit in enumerate(vector_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + vw / (_RRF_K + rank)
        payload[cid] = hit[:4]
        if len(hit) > 4 and hit[4] is not None:
            sims[cid] = float(hit[4])
    for rank, hit in enumerate(keyword_hits):
        cid = hit[1]
        scores[cid] = scores.get(cid, 0.0) + kw / (_RRF_K + rank)
        payload.setdefault(cid, hit[:4])
    fused = [(*payload[cid], sc, sims.get(cid)) for cid, sc in scores.items()]
    fused.sort(key=lambda x: x[4], reverse=True)
    return fused


async def _recheck_keyword_only(
    db: AsyncSession,
    emb_cfg: EmbeddingSettings,
    qvec: list[float],
    query: str,
    fused: list[tuple],
    floor: float,
) -> list[tuple]:
    """Drop keyword-only rows (f[5] is None) whose query-vector cosine is below `floor`.

    The tsv/trgm gate is boolean — a chunk sharing one generic token qualifies — and
    with rerank unavailable nothing downstream would catch it. The recheck is cheap:
    a direct-by-id similarity SQL, no ANN scan. Exempt from the floor:
      - hits containing one of the query's code tokens (case-insensitive): exact-term
        recall legitimately scores low on cosine and is the keyword path's raison d'être;
      - rows whose similarity can't be computed (no comparable embedding) — benefit of
        the doubt, consistent with keyword search covering not-yet-embedded chunks.
    Only called when qvec is available; embedding-down mode keeps keyword-only intact."""
    kw_only = [f for f in fused if f[5] is None]
    if not kw_only:
        return fused
    codes = [t.lower() for t in _CODE_TOKEN.findall(query)]
    to_check = [
        f for f in kw_only if not any(c in f[3].lower() for c in codes)
    ]
    if not to_check:
        return fused
    try:
        sql = text(
            """
            SELECT c.id::text, 1 - (c.embedding <=> CAST(:qvec AS vector)) AS sim
            FROM knowledge_chunks c
            WHERE c.id IN :ids
              AND c.embedding IS NOT NULL
              AND c.status = 'ready'
              AND c.embedding_dim = :dim
              AND (c.embedding_model = :model OR c.embedding_model = '')
            """
        ).bindparams(bindparam("ids", expanding=True))
        rows = (
            await db.execute(
                sql,
                {
                    "qvec": _vec_literal(qvec),
                    "dim": emb_cfg.dim,
                    "model": emb_cfg.model,
                    "ids": [uuid.UUID(f[1]) for f in to_check],
                },
            )
        ).all()
        sims = {r[0]: float(r[1]) for r in rows if r[1] is not None}
    except Exception as exc:  # noqa: BLE001 — recheck is a refinement, never break retrieval
        log.warning("keyword_recheck_degraded", error=str(exc))
        return fused
    drop = {f[1] for f in to_check if f[1] in sims and sims[f[1]] < floor}
    if drop:
        log.info("keyword_only_hits_filtered", dropped=len(drop), floor=floor)
    return [f for f in fused if f[1] not in drop]


async def _chunk_indexes(db: AsyncSession, chunk_ids: list[str]) -> dict[str, int]:
    """{chunk_id: chunk_index} for span merging; {} on failure (merging degrades to no-op)."""
    if not chunk_ids:
        return {}
    try:
        sql = text(
            "SELECT id::text, chunk_index FROM knowledge_chunks WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows = (
            await db.execute(sql, {"ids": [uuid.UUID(c) for c in chunk_ids]})
        ).all()
        return {r[0]: int(r[1]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("chunk_index_lookup_degraded", error=str(exc))
        return {}


def _merge_adjacent(fused: list[tuple], idx_map: dict[str, int]) -> list[_Group]:
    """Collapse same-item hits with adjacent/overlapping chunk_index into one group.

    Left separate, such hits waste top_k slots on copies of the same passage (their
    expanded contexts are ~2/3 identical). `fused` is score-descending, so a group's
    first member is its rep and the group list stays score-descending."""
    groups: list[_Group] = []
    by_item: dict[str, list[_Group]] = {}
    for row in fused:
        ci = idx_map.get(row[1])
        if ci is None:
            groups.append(_Group(row, None, None))
            continue
        mine = by_item.setdefault(row[0], [])
        near = [g for g in mine if g.lo - _MERGE_GAP <= ci <= g.hi + _MERGE_GAP]
        if not near:
            g = _Group(row, ci, ci)
            groups.append(g)
            mine.append(g)
            continue
        base = near[0]  # created earliest ⇒ best-scoring rep of the runs being joined
        base.lo = min(base.lo, ci)
        base.hi = max(base.hi, ci)
        for other in near[1:]:  # this hit bridged previously separate runs
            base.lo = min(base.lo, other.lo)
            base.hi = max(base.hi, other.hi)
            groups.remove(other)
            mine.remove(other)
    return groups


def _apply_item_quota(
    groups: list[_Group], per_item_max: int, top_k: int
) -> list[_Group]:
    """Soft per-item cap over the score-ranked groups, then truncate to top_k.

    "Soft": an item's surplus groups are deprioritised, not discarded — they still fill
    top_k when no other item has candidates left, so a query whose answer genuinely
    spans one long document isn't truncated. per_item_max <= 0 disables the cap."""
    if per_item_max <= 0:
        return groups[:top_k]
    kept: list[_Group] = []
    surplus: list[_Group] = []
    counts: dict[str, int] = {}
    for g in groups:
        item = g.rep[0]
        if counts.get(item, 0) < per_item_max:
            counts[item] = counts.get(item, 0) + 1
            kept.append(g)
        else:
            surplus.append(g)
    if len(kept) < top_k:
        kept.extend(surplus[: top_k - len(kept)])
    return kept[:top_k]


async def _expand_context(db: AsyncSession, groups: list[_Group]) -> dict[str, str]:
    """Small-to-big: {rep chunk_id: group's chunk span merged with its edge neighbours}.

    Retrieval matches small chunks (precise), but a 600-char slice can sever the
    answer. Each group is expanded ONCE over its merged [lo-1, hi+1] chunk_index range;
    per-hit expansion of adjacent hits would inject the same passage 2-3 times."""
    spans = [
        {"cid": g.rep[1], "item_id": g.rep[0], "lo": g.lo, "hi": g.hi}
        for g in groups
        if g.lo is not None and g.hi is not None
    ]
    if not spans:
        return {}
    try:
        sql = text(
            """
            SELECT g.cid,
                   string_agg(c.content, chr(10) ORDER BY c.chunk_index) AS merged
            FROM jsonb_to_recordset(CAST(:spans AS jsonb))
                 AS g(cid text, item_id uuid, lo int, hi int)
            JOIN knowledge_chunks c
              ON c.item_id = g.item_id
             AND c.chunk_index BETWEEN g.lo - 1 AND g.hi + 1
            GROUP BY g.cid
            """
        )
        rows = (await db.execute(sql, {"spans": json.dumps(spans)})).all()
        return {r[0]: r[1] for r in rows if r[1]}
    except Exception as exc:  # noqa: BLE001 — expansion is a bonus, never break retrieval
        log.warning("context_expand_degraded", error=str(exc))
        return {}


async def hybrid_search(
    db: AsyncSession, cfg: AIConfig, query: str, top_k: int | None = None
) -> list[RetrievalResult]:
    params = merged_retrieval(cfg)  # defaults + DB (DB wins; nulls fall back to defaults)
    top_k = top_k or int(params["top_k"])
    vw = float(params["vector_weight"])
    kw = float(params["keyword_weight"])
    vector_min_sim = float(params["vector_min_sim"])
    min_score = float(params["min_score"])
    trgm_threshold = float(params["trgm_threshold"])
    keyword_min_sim = float(params["keyword_min_sim"])
    per_item_max = int(params["per_item_max"])
    candidate_multiplier = int(params["candidate_multiplier"])
    candidate_n = max(top_k * candidate_multiplier, 20)
    tenant = str(getattr(cfg, "tenant_id", None) or "unknown")

    # NFKC folds the full-width alphanumerics Chinese IMEs produce (ＡＸ３０００ →
    # AX3000) so tsv, trigram and the embedding all see the documents' width. Stored
    # document content is untouched — rag.segment normalises the content_seg side.
    query = unicodedata.normalize("NFKC", query)

    emb_cfg = to_embedding_settings(cfg)
    qvec = await _embed_query(emb_cfg, query)
    vector_hits: list[tuple[str, str, str, str, float]] = []
    vector_top_sim: float | None = None
    if qvec is not None:
        vector_hits, vector_top_sim = await _vector_search(
            db, emb_cfg, qvec, candidate_n, vector_min_sim
        )
    keyword_hits = await _keyword_search(db, query, candidate_n, trgm_threshold)

    if vector_top_sim is not None:
        retrieval_top_score.labels(kind="vector_sim").observe(vector_top_sim)

    if not vector_hits and not keyword_hits:
        retrieval_calls.labels(outcome="miss", tenant=tenant).inc()
        log.info(
            "retrieval_miss",
            query=query,
            vector_top_sim=vector_top_sim,
            keyword_hits=0,
        )
        return []

    fused = _fuse(vector_hits, keyword_hits, vw, kw)

    # Optional rerank over the fused candidates (on the matched chunk text).
    rer_cfg = to_rerank_settings(cfg)
    reranked = False
    # Rerank even a single candidate: otherwise that result carries a fusion score
    # (~0.017) where every other one carries a rerank score, min_score never applies
    # to it, and dashboards / calibration see two incomparable scales.
    if rer_cfg and rer_cfg.api_key and fused:
        top_n = max(int(params["rerank_top_n"]), top_k)
        pool = fused[:candidate_n]
        docs = [f"{f[2]}\n{f[3]}" for f in pool]
        order = await get_rerank_client(rer_cfg).rerank(query, docs, top_n)
        # All-zero scores ⇒ the client degraded (failure); keep the fusion order.
        if order and any(sc > 0 for _, sc in order):
            reranked = True
            fused = [(*pool[i][:4], sc, sc) for i, sc in order]

    # Relevance floor. With a successful rerank, min_score is the calibrated final gate.
    if reranked and min_score > 0:
        fused = [f for f in fused if f[4] >= min_score]
    if not reranked:
        # No rerank (off, or degraded at runtime) ⇒ the fusion order is final, so both
        # paths need an explicit floor here:
        #  - vector rows (f[5] = cosine sim): _vector_search pre-gates at the same
        #    threshold today, making this a no-op — it exists so a future relaxed
        #    pre-gate (e.g. feeding rerank a wider pool) can never leak sub-floor rows
        #    into top_k when the rerank client degrades;
        #  - keyword-only rows (f[5] = None) cleared only a boolean match and get a
        #    looser vector recheck (skipped entirely when embeddings are down).
        fused = [f for f in fused if f[5] is None or f[5] >= vector_min_sim]
        if qvec is not None and keyword_min_sim > 0:
            fused = await _recheck_keyword_only(
                db, emb_cfg, qvec, query, fused, keyword_min_sim
            )

    groups: list[_Group] = []
    if fused:
        idx_map = await _chunk_indexes(db, [f[1] for f in fused])
        groups = _apply_item_quota(_merge_adjacent(fused, idx_map), per_item_max, top_k)
        groups = _collapse_question_hits(groups)

    if not groups:
        retrieval_calls.labels(outcome="filtered", tenant=tenant).inc()  # recalled, nothing cleared the floor
        log.info(
            "retrieval_filtered",
            query=query,
            vector_top_sim=vector_top_sim,
            keyword_hits=len(keyword_hits),
        )
        return []

    retrieval_calls.labels(outcome="hit", tenant=tenant).inc()
    score_kind = "rerank" if reranked else "fusion"
    top_score = max(float(g.rep[4]) for g in groups)
    retrieval_top_score.labels(kind=score_kind).observe(top_score)
    log.info(
        "retrieval_hit",
        query=query,
        vector_top_sim=vector_top_sim,
        keyword_hits=len(keyword_hits),
        top_score=round(top_score, 4),
        score_kind=score_kind,
        results=len(groups),
    )

    body_groups = [g for g in groups if not is_question_index(g.lo)]
    question_groups = [g for g in groups if is_question_index(g.lo)]
    expanded = await _expand_context(db, body_groups) if params["expand_context"] else {}
    bodies = await _item_bodies(db, [g.rep[0] for g in question_groups]) if question_groups else {}

    out: list[RetrievalResult] = []
    for g in groups:
        if is_question_index(g.lo):
            # A customer phrasing matched: cite and inject the item's body, not the
            # question text (which is what the vector/FTS row holds).
            first, body = bodies.get(g.rep[0], (g.rep[3], g.rep[3]))
            out.append(
                RetrievalResult(
                    item_id=g.rep[0], chunk_id=g.rep[1], title=g.rep[2], content=first,
                    score=round(float(g.rep[4]), 4), context=body, score_kind=score_kind,
                    matched_question=g.rep[3],
                )
            )
        else:
            out.append(
                RetrievalResult(
                    item_id=g.rep[0], chunk_id=g.rep[1], title=g.rep[2], content=g.rep[3],
                    score=round(float(g.rep[4]), 4), context=expanded.get(g.rep[1], g.rep[3]),
                    score_kind=score_kind,
                )
            )
    return out


def _collapse_question_hits(groups: list[_Group]) -> list[_Group]:
    """One result per item once a question chunk of that item matched.

    A question hit and a body hit of the same item would otherwise occupy two top_k
    slots and inject the same body twice; keep the best-scoring one (groups arrive
    score-descending) and let the question's score stand."""
    with_question = {g.rep[0] for g in groups if is_question_index(g.lo)}
    out: list[_Group] = []
    seen: set[str] = set()
    for g in groups:
        item = g.rep[0]
        if item in with_question:
            if item in seen:
                continue
            seen.add(item)
        out.append(g)
    return out


_BODY_CHARS = 1500


async def _item_bodies(db: AsyncSession, item_ids: list[str]) -> dict[str, tuple[str, str]]:
    """{item_id: (first body chunk, body chunks joined up to _BODY_CHARS)} for question hits."""
    if not item_ids:
        return {}
    try:
        sql = text(
            """
            SELECT item_id::text, content
            FROM knowledge_chunks
            WHERE item_id IN :ids AND chunk_index < :base
            ORDER BY item_id, chunk_index
            """
        ).bindparams(bindparam("ids", expanding=True))
        rows = (
            await db.execute(
                sql, {"ids": [uuid.UUID(i) for i in dict.fromkeys(item_ids)], "base": QUESTION_INDEX_BASE}
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 — fall back to the question text itself
        log.warning("item_body_lookup_degraded", error=str(exc))
        return {}
    out: dict[str, tuple[str, str]] = {}
    for item_id, content in rows:
        first, body = out.get(item_id, ("", ""))
        if not first:
            first = content
        if len(body) < _BODY_CHARS:
            body = (body + "\n" + content).strip() if body else content
        out[item_id] = (first, body[:_BODY_CHARS])
    return out
