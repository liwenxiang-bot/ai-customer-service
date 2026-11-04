"""Unit: customer phrasings (rag.questions) — normalisation, LLM output parsing, and the
retrieval-side collapse of question hits."""

from __future__ import annotations

from app.rag.questions import (
    QUESTION_INDEX_BASE,
    is_question_index,
    normalize_questions,
    parse_generated,
)
from app.rag.retrieval import _collapse_question_hits, _Group


def test_normalize_dedupes_trims_and_excludes_title():
    out = normalize_questions(
        ["  怎么退款？", "怎么退款?", "1. 买错了能退吗", "", "x" * 200, "退款政策", "买错了能退吗"],
        exclude=["退款政策"],
    )
    assert out == ["怎么退款？", "买错了能退吗"]
    assert len(normalize_questions([f"问{i}" for i in range(50)])) == 12


def test_parse_generated_json_fence_and_plain_lines():
    assert parse_generated('```json\n["a", "b"]\n```') == ["a", "b"]
    assert parse_generated('{"questions": ["x"]}') == ["x"]
    assert parse_generated("- 第一\n- 第二\n") == ["第一", "第二"]
    assert parse_generated("") == []


def test_question_index_split():
    assert not is_question_index(0) and not is_question_index(None)
    assert is_question_index(QUESTION_INDEX_BASE) and is_question_index(QUESTION_INDEX_BASE + 5)


def _g(item, chunk, score, lo):
    return _Group((item, chunk, "t", "c", score, None), lo, lo)


def test_collapse_keeps_one_result_per_item_when_a_question_matched():
    groups = [
        _g("A", "qa", 0.9, QUESTION_INDEX_BASE),  # question hit, best
        _g("B", "b1", 0.5, 0),
        _g("A", "a1", 0.4, 0),  # body hit of the same item → dropped
        _g("C", "c1", 0.3, 2),
        _g("C", "c2", 0.2, 7),  # two body hits of C, no question → both stay
    ]
    out = _collapse_question_hits(groups)
    assert [(g.rep[0], g.rep[1]) for g in out] == [("A", "qa"), ("B", "b1"), ("C", "c1"), ("C", "c2")]
