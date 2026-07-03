"""Unit: text chunking (overlap, boundary handling, mixed CJK/latin)."""

from app.rag.chunking import chunk_text


def test_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_single_short_chunk():
    out = chunk_text("这是一段很短的文本。", chunk_size=600, overlap=100)
    assert len(out) == 1
    assert "这是一段很短的文本" in out[0]


def test_multiple_chunks_with_overlap():
    text = "。".join(f"第{i}句话内容" for i in range(200))
    out = chunk_text(text, chunk_size=200, overlap=40)
    assert len(out) > 1
    # Each chunk respects the size budget (allow some slack for boundaries).
    assert all(len(c) <= 240 for c in out)


def test_oversized_unit_is_hard_split():
    out = chunk_text("x" * 1500, chunk_size=500, overlap=50)
    assert len(out) >= 3
    assert all(c.strip() for c in out)


def test_paragraph_boundaries_preserved():
    text = "段落一的内容。\n\n段落二的内容。\n\n段落三的内容。"
    out = chunk_text(text, chunk_size=1000, overlap=0)
    assert len(out) == 1  # all fit in one chunk
    assert "段落一" in out[0] and "段落三" in out[0]


def test_overlap_is_whole_sentences():
    text = "。".join(f"句子{i}的内容比较长一些" for i in range(40)) + "。"
    out = chunk_text(text, chunk_size=150, overlap=50)
    assert len(out) > 2
    for c in out[1:]:
        # Carried overlap starts at a sentence boundary, not a mid-sentence byte slice.
        assert c.startswith("句子")


def test_semicolon_splits_long_clause_chains():
    text = "条款甲的完整描述；" * 60
    out = chunk_text(text, chunk_size=200, overlap=40)
    # Sentence-level splitting on '；' keeps chunks within budget without hard-splitting.
    assert all(len(c) <= 240 for c in out)
    assert all(not c.startswith("；") for c in out)


def test_no_fragment_chunks():
    text = "。".join(f"第{i}句话的具体内容说明" for i in range(80)) + "。"
    out = chunk_text(text, chunk_size=200, overlap=40)
    # No chunk should be mostly-overlap noise or a tiny hard-split remainder.
    assert min(len(c) for c in out) >= 40


def test_hard_split_tail_is_folded():
    out = chunk_text("x" * 1030, chunk_size=500, overlap=50)
    assert all(len(c) > 50 for c in out)  # tail slice ≤ overlap would be pure duplicate
    assert sum(len(c) for c in out) >= 1030  # nothing lost


def test_pure_overlap_tail_not_emitted():
    # Two sentences that exactly fill one chunk; the carried overlap alone must not
    # become a trailing duplicate chunk.
    text = "前一句内容。后一句内容。"
    out = chunk_text(text, chunk_size=len(text) + 2, overlap=6)
    assert len(out) == 1
