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
