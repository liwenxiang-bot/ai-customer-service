"""Retrieval quality: unit checks (fusion/segment/param merge) + a DB-backed gate.

The unit half needs nothing. The gate seeds scripts.rag_eval's labelled corpus into
the configured Postgres (CI provides pgvector + migrations) and holds the keyword
path to a recall/rejection floor, so regressions in chunking, segmentation, keyword
SQL, fusion or the relevance gates fail CI instead of silently eroding answer
quality. The vector path is deliberately forced off (embedding key emptied): CI has
no API key, and the keyword path is the only one that can be asserted
deterministically. It skips — never fails — when Postgres is unreachable or the KB
already has published items (results would be polluted), so a DB-less or dev-DB
`pytest` run stays green.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy import text as sql_text

from app.rag.retrieval import _fuse
from app.rag.segment import available, segment
from app.services.ai_config import RETRIEVAL_DEFAULTS, merged_retrieval

# ---------------------------------------------------------------- unit (no DB)


def test_segment_empty():
    assert segment("") == ""
    assert segment("   ") == ""


def test_segment_returns_text():
    out = segment("退货政策是什么")
    assert isinstance(out, str) and out
    # With jieba installed the output is space-tokenised; otherwise it's the input.
    if available():
        assert " " in out


def test_segment_keeps_codes():
    out = segment("错误码E1001怎么处理")
    assert "E1001" in out.replace(" ", "")


def test_merged_retrieval_fills_defaults_and_ignores_null():
    cfg = SimpleNamespace(retrieval={"top_k": 8, "vector_min_sim": None, "min_score": 0})
    m = merged_retrieval(cfg)
    assert m["top_k"] == 8                                              # DB value wins
    assert m["vector_min_sim"] == RETRIEVAL_DEFAULTS["vector_min_sim"]  # null → default
    assert m["expand_context"] == RETRIEVAL_DEFAULTS["expand_context"]  # missing → default
    assert m["min_score"] == 0                                          # explicit 0 honoured
    assert set(RETRIEVAL_DEFAULTS).issubset(m)                          # every key present


def test_fuse_propagates_vector_similarity():
    # 5-tuples carry the cosine similarity: (item, chunk, title, content, sim)
    v = [("i1", "c1", "t1", "x", 0.9), ("i2", "c2", "t2", "y", 0.7)]
    k = [("i2", "c2", "t2", "y"), ("i3", "c3", "t3", "z")]
    fused = _fuse(v, k, vw=0.6, kw=0.4)
    by_id = {f[1]: f for f in fused}
    # c2 appears in both lists → should rank first.
    assert fused[0][1] == "c2"
    # sim is carried from the vector hit; the keyword-only c3 has none.
    assert by_id["c1"][5] == 0.9
    assert by_id["c3"][5] is None


def test_fuse_backward_compatible_with_4tuples():
    v = [("i1", "c1", "t1", "x")]
    k = [("i2", "c2", "t2", "y")]
    fused = _fuse(v, k, 0.6, 0.4)
    assert {f[1] for f in fused} == {"c1", "c2"}
    assert all(f[5] is None for f in fused)


# ------------------------------------------- keyword-path quality gate (DB-backed)

# Floors are deliberately loose: the gate exists to catch catastrophic regressions
# (a broken tsquery, an over-eager stopword list, a relevance gate eating true hits),
# not to tune parameters. On an empty KB the current pipeline scores 8/8 and 4/4.
MIN_RECALL_AT_K = 0.75
MIN_NEGATIVE_REJECTION = 0.75


@pytest.fixture
async def gate_db():
    """Session against the configured Postgres, or skip when it isn't usable.

    "Usable" = reachable AND migrated AND an empty knowledge base: seeded eval items
    compete with whatever else is published, so a populated dev KB would make both
    recall and rejection nondeterministic.
    """
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        published = (
            await session.execute(
                sql_text("SELECT count(*) FROM knowledge_items WHERE status = 'published'")
            )
        ).scalar_one()
    except Exception as exc:  # noqa: BLE001 — unreachable/unmigrated DB
        await session.close()
        pytest.skip(f"Postgres unavailable for the retrieval quality gate: {exc}")
    if published:
        await session.close()
        pytest.skip(
            f"knowledge base not empty ({published} published items) — "
            "the quality gate needs an isolated DB (CI provides one)"
        )
    try:
        yield session
    finally:
        await session.close()


async def test_keyword_path_quality_gate(gate_db, monkeypatch):
    if not available():
        pytest.skip("jieba unavailable — the keyword path under test would be degraded")

    from app.config import settings
    from app.models.config import AIConfig
    from app.models.knowledge import KnowledgeChunk, KnowledgeItem
    from app.rag.chunking import chunk_text
    from app.rag.retrieval import hybrid_search
    from scripts.rag_eval import CORPUS, NEGATIVE_CASES, POSITIVE_CASES, TOP_K

    # Force the vector path off even on a dev machine with a configured key: the gate
    # must be deterministic and free. (A transient AIConfig has no encrypted key, so
    # to_embedding_settings falls back to this setting.)
    monkeypatch.setattr(settings, "embedding_api_key", "")
    cfg = AIConfig(rerank_enabled=False, retrieval={})

    db = gate_db
    run_tag = f"[rag-eval-test-{uuid.uuid4().hex[:8]}]"
    params = merged_retrieval(cfg)
    try:
        for title, content in CORPUS:
            item = KnowledgeItem(
                title=f"{run_tag} {title}", content=content,
                category="eval-test", status="published",
            )
            db.add(item)
            await db.flush()
            pieces = chunk_text(
                content, int(params["chunk_size"]), int(params["chunk_overlap"])
            )
            for i, piece in enumerate(pieces):
                db.add(KnowledgeChunk(
                    tenant_id=item.tenant_id, item_id=item.id, chunk_index=i,
                    content=piece,
                    # Title prefixed per the ingest rule so title words are
                    # keyword-searchable from every chunk.
                    content_seg=segment(f"{item.title}\n{piece}"),
                    embedding=None, embedding_model="", embedding_dim=0,
                    status="pending", token_count=len(piece),
                ))
        await db.commit()

        hits = 0
        misses: list[str] = []
        for query, expected in POSITIVE_CASES:
            results = await hybrid_search(db, cfg, query, top_k=TOP_K)
            if any(expected in (r.title or "") for r in results):
                hits += 1
            else:
                misses.append(f"{query!r} → {[r.title for r in results]}")
        rejected = 0
        leaks: list[str] = []
        for query in NEGATIVE_CASES:
            results = await hybrid_search(db, cfg, query, top_k=TOP_K)
            if results:
                leaks.append(f"{query!r} → {[r.title for r in results]}")
            else:
                rejected += 1

        recall = hits / len(POSITIVE_CASES)
        rejection = rejected / len(NEGATIVE_CASES)
        assert recall >= MIN_RECALL_AT_K, (
            f"keyword recall@{TOP_K} regressed: {recall:.0%} < {MIN_RECALL_AT_K:.0%}; "
            f"missed: {misses}"
        )
        assert rejection >= MIN_NEGATIVE_REJECTION, (
            f"negative rejection regressed: {rejection:.0%} < {MIN_NEGATIVE_REJECTION:.0%}; "
            f"leaked: {leaks}"
        )
    finally:
        # rollback first so cleanup still runs if the failure aborted the transaction.
        await db.rollback()
        await db.execute(
            delete(KnowledgeItem).where(KnowledgeItem.title.like(f"{run_tag}%"))
        )
        await db.commit()
