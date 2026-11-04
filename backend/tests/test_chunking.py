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


# ----------------------------------------------------------------- Markdown structure

from app.rag.chunking import chunk_markdown  # noqa: E402


def test_cjk_sentences_rejoin_without_inserted_spaces():
    out = chunk_text("第一句。第二句！第三句？", chunk_size=600, overlap=0)
    assert out == ["第一句。第二句！第三句？"]


def test_list_items_keep_their_line_breaks():
    text = "## 步骤\n\n- 第一步做这个\n- 第二步做那个\n- 第三步完成"
    out = chunk_text(text, chunk_size=600, overlap=0)
    assert out == [text]


def test_table_and_code_block_are_atomic():
    table = "| 列A | 列B |\n|---|---|\n" + "\n".join(f"| 行{i} | 值{i} |" for i in range(12))
    code = "```bash\n" + "\n".join(f"echo line{i}" for i in range(8)) + "\n```"
    text = f"# 指南\n\n前言一句。\n\n{table}\n\n中间一句。\n\n{code}\n\n结尾一句。"
    out = chunk_text(text, chunk_size=260, overlap=30)
    joined = "\n".join(out)
    assert table in joined and code in joined  # neither block was cut
    assert all(c.count("```") in (0, 2) for c in out)  # fences never straddle chunks


def test_oversized_table_splits_between_rows():
    rows = "\n".join(f"| 行{i} | 这是第{i}行的值，内容略长一些 |" for i in range(40))
    out = chunk_text("| A | B |\n|---|---|\n" + rows, chunk_size=300, overlap=50)
    assert len(out) > 1
    for c in out:
        assert all(ln.startswith("|") and ln.endswith("|") for ln in c.split("\n"))


def test_heading_never_ends_a_chunk_and_path_is_tracked():
    text = (
        "# 手册\n\n" + "正文一。" * 60 + "\n\n## 安装\n\n" + "安装步骤。" * 60
        + "\n\n### Windows\n\n" + "点击下一步。" * 60
    )
    chunks = chunk_markdown(text, chunk_size=200, overlap=40)
    for c in chunks:
        assert not c.text.rstrip().split("\n")[-1].startswith("#")
    paths = [c.heading for c in chunks]
    assert "手册" in paths and "手册 > 安装" in paths and "手册 > 安装 > Windows" in paths


def test_heading_into_a_full_chunk_starts_the_next_without_overlap():
    text = "a。" * 95 + "\n\n## B\n\n" + "b。" * 100
    chunks = chunk_markdown(text, chunk_size=200, overlap=40)
    second = next(c for c in chunks if "## B" in c.text)
    assert second.text.startswith("## B")  # no carried tail in front of a section start
    assert second.heading == "B"


def test_chunk_text_still_returns_strings():
    out = chunk_text("# T\n\n正文。", 600, 100)
    assert out == ["# T\n\n正文。"]
