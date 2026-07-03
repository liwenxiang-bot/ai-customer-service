"""Chinese word segmentation for the keyword (full-text) retrieval path.

Postgres' built-in 'simple' text-search config does not segment Chinese: a CJK run has
no spaces, so a whole sentence collapses into ~one lexeme and tsvector matching all but
fails. We segment with jieba on BOTH sides — documents at ingest time (stored in
`knowledge_chunks.content_seg`) and the query at search time — so 'simple' sees
space-delimited words it can index and match.

Both sides are NFKC-normalised first: Chinese IMEs produce full-width alphanumerics
(ＡＸ３０００, ｅ１００１) that 'simple' and pg_trgm treat as different characters from
their half-width document form. Only the derived tokens are affected — the stored
chunk content stays raw, so citations remain verbatim.

`cut_for_search` is used deliberately: it emits both long and short tokens
(中华人民共和国 → 中华/华人/人民/共和/共和国/中华人民共和国), which favours recall —
precision is handled downstream by the vector floor, rerank and min_score. The QUERY
side additionally drops stopwords and lone CJK characters (see `segment_query`): under
OR-matching those turn every chunk sharing a function word into a candidate.

Domain terms jieba doesn't know (product names, brand slang) can be supplied via a
user dictionary (`RAG_JIEBA_USERDICT`), loaded once at import; documents ingested
before a dictionary change need a resegment pass to pick it up.

Degrades gracefully: if jieba can't be imported we return the text unchanged, so the
pipeline still works (keyword search falls back to trigram word-similarity matching).
"""

from __future__ import annotations

import logging
import re
import unicodedata

from app.config import settings
from app.core.logging import get_logger

log = get_logger("rag.segment")

try:
    import jieba

    # Suppress jieba's first-run "Building prefix dict…" chatter.
    jieba.setLogLevel(logging.WARNING)
    if settings.rag_jieba_userdict:
        try:
            jieba.load_userdict(settings.rag_jieba_userdict)
        except Exception as exc:  # noqa: BLE001 — a bad dict path must not take retrieval down
            log.warning(
                "jieba_userdict_load_failed",
                path=settings.rag_jieba_userdict,
                error=str(exc),
            )
    _JIEBA_OK = True
except Exception as exc:  # pragma: no cover — jieba is a declared dep; this is belt-and-braces
    jieba = None  # type: ignore[assignment]
    _JIEBA_OK = False
    log.warning("jieba_unavailable_keyword_search_degraded", error=str(exc))

_ALNUM = re.compile(r"[A-Za-z0-9]")

# Query-side stopwords. Deliberately small and conservative: question/aux/tone/function
# words only, never anything that can be the sole content word of a short query
# (「退」「货」「钱」 stay). Applied to queries only — documents keep full segmentation.
_STOPWORDS = frozenset(
    {
        "的", "了", "吗", "呢", "吧", "啊", "呀", "哦", "嘛", "嗯", "哈",
        "是", "在", "有", "和", "与", "或", "就", "都", "也", "还", "又",
        "被", "把", "对", "给", "让", "从", "到", "于", "着",
        "我", "你", "您", "我们", "你们", "他们",
        "这", "那", "这个", "那个", "这样", "那样",
        "什么", "怎么", "怎样", "如何", "为什么", "哪个", "哪些", "多少",
        "可以", "能", "会", "要", "想", "请", "请问", "麻烦", "帮我", "一下",
        "没有", "的话", "还是", "或者", "如果", "一个",
    }
)


def segment(text: str) -> str:
    """Re-join `text` as space-delimited tokens for ``to_tsvector('simple', …)``.

    Latin words and codes (E1001, SKU-12) are kept intact by jieba. Empty/whitespace
    input returns ``""``; on any failure the original text is returned unchanged.
    """
    if not text or not text.strip():
        return ""
    text = unicodedata.normalize("NFKC", text)  # full-width → half-width (ＥＲＲ０１ → ERR01)
    if not _JIEBA_OK:
        return text
    try:
        tokens = (t.strip() for t in jieba.cut_for_search(text))
        joined = " ".join(t for t in tokens if t)
        return joined or text
    except Exception:  # noqa: BLE001 — never let segmentation break ingest/search
        return text


def segment_query(text: str) -> str:
    """OR-join the query's meaningful tokens for ``websearch_to_tsquery('simple', …)``.

    Document side uses ``segment`` (space-joined → AND-ish coverage). But on the QUERY
    side, jieba's many sub-tokens (退货政策→退货/政策/退货政策…) under the default AND mean a
    chunk must contain *every* sub-token to match — far too strict for Chinese, so the
    high-quality FTS signal all but vanishes on multi-word queries. OR-joining lets a chunk
    matching *any* meaningful token become a candidate; precision is restored downstream by
    the vector floor, RRF fusion and rerank. (``websearch_to_tsquery`` reads ``or`` as ``|``
    and sanitises odd tokens, so this stays injection-safe.)

    OR-matching makes noise tokens expensive, so unlike ``segment`` this filters:
    stopwords are dropped, and lone CJK characters are dropped when a multi-char token
    remains to carry the query (「怎么退」 must keep 「退」). Alphanumeric tokens (E1001,
    X1) always survive. A query reduced to nothing returns ``""`` — matching *something*
    on pure function words would only be noise.
    """
    if not text or not text.strip():
        return ""
    text = unicodedata.normalize("NFKC", text)
    if not _JIEBA_OK:
        return text
    try:
        seen: set[str] = set()
        toks: list[str] = []
        for t in jieba.cut_for_search(text):
            t = t.strip()
            if t and t.lower() != "or" and t not in seen and t not in _STOPWORDS:
                seen.add(t)
                toks.append(t)
        if any(len(t) > 1 for t in toks):
            toks = [t for t in toks if len(t) > 1 or _ALNUM.search(t)]
        return " or ".join(toks)
    except Exception:  # noqa: BLE001 — never let segmentation break search
        return text


def available() -> bool:
    """Whether jieba-backed segmentation is active (False → trigram-only fallback)."""
    return _JIEBA_OK
