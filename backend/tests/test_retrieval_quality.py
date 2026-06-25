"""Unit: jieba segmentation + RRF fusion / param helpers (no DB required)."""

from types import SimpleNamespace

from app.rag.retrieval import _fuse
from app.rag.segment import available, segment
from app.services.ai_config import RETRIEVAL_DEFAULTS, merged_retrieval


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
