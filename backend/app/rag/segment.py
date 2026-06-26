"""Chinese word segmentation for the keyword (full-text) retrieval path.

Postgres' built-in 'simple' text-search config does not segment Chinese: a CJK run has
no spaces, so a whole sentence collapses into ~one lexeme and tsvector matching all but
fails. We segment with jieba on BOTH sides — documents at ingest time (stored in
`knowledge_chunks.content_seg`) and the query at search time — so 'simple' sees
space-delimited words it can index and match.

`cut_for_search` is used deliberately: it emits both long and short tokens
(中华人民共和国 → 中华/华人/人民/共和/共和国/中华人民共和国), which favours recall —
precision is handled downstream by the vector floor, rerank and min_score.

Degrades gracefully: if jieba can't be imported we return the text unchanged, so the
pipeline still works (keyword search falls back to trigram substring matching).
"""

from __future__ import annotations

import logging

from app.core.logging import get_logger

log = get_logger("rag.segment")

try:
    import jieba

    # Suppress jieba's first-run "Building prefix dict…" chatter.
    jieba.setLogLevel(logging.WARNING)
    _JIEBA_OK = True
except Exception as exc:  # pragma: no cover — jieba is a declared dep; this is belt-and-braces
    jieba = None  # type: ignore[assignment]
    _JIEBA_OK = False
    log.warning("jieba_unavailable_keyword_search_degraded", error=str(exc))


def segment(text: str) -> str:
    """Re-join `text` as space-delimited tokens for ``to_tsvector('simple', …)``.

    Latin words and codes (E1001, SKU-12) are kept intact by jieba. Empty/whitespace
    input returns ``""``; on any failure the original text is returned unchanged.
    """
    if not text or not text.strip():
        return ""
    if not _JIEBA_OK:
        return text
    try:
        tokens = (t.strip() for t in jieba.cut_for_search(text))
        joined = " ".join(t for t in tokens if t)
        return joined or text
    except Exception:  # noqa: BLE001 — never let segmentation break ingest/search
        return text


def segment_query(text: str) -> str:
    """OR-join the query's tokens for ``websearch_to_tsquery('simple', …)``.

    Document side uses ``segment`` (space-joined → AND-ish coverage). But on the QUERY
    side, jieba's many sub-tokens (退货政策→退货/政策/退货政策…) under the default AND mean a
    chunk must contain *every* sub-token to match — far too strict for Chinese, so the
    high-quality FTS signal all but vanishes on multi-word queries. OR-joining lets a chunk
    matching *any* meaningful token become a candidate; precision is restored downstream by
    the vector floor, RRF fusion and rerank. (``websearch_to_tsquery`` reads ``or`` as ``|``
    and sanitises odd tokens, so this stays injection-safe.)
    """
    if not text or not text.strip():
        return ""
    if not _JIEBA_OK:
        return text
    try:
        seen: set[str] = set()
        toks: list[str] = []
        for t in jieba.cut_for_search(text):
            t = t.strip()
            if t and t.lower() != "or" and t not in seen:
                seen.add(t)
                toks.append(t)
        return " or ".join(toks) or text
    except Exception:  # noqa: BLE001 — never let segmentation break search
        return text


def available() -> bool:
    """Whether jieba-backed segmentation is active (False → trigram-only fallback)."""
    return _JIEBA_OK
