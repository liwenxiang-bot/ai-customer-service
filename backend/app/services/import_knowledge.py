"""Batch knowledge import (CSV / JSON).

Parsing + item insert is fast and synchronous; the slow part (embedding) is enqueued
per item, so importing hundreds of rows never blocks the request (requirements §7, §13).
Returns per-row success/failure so the admin UI can show what went wrong.

Import hygiene matters more here than in the editor, because a bad batch silently
poisons retrieval for a long time:
- bytes are decoded strictly (UTF-8 first, then GB18030 — Excel's default CSV encoding
  on Chinese Windows) so mojibake never reaches the embedding index unnoticed;
- HTML-looking content is stripped to plain text before storage (CMS exports carry
  <p>/<div>/&nbsp; noise into vectors and FTS tokens); Markdown is kept verbatim —
  it is the editor's designed storage format and embeddings are robust to it;
- duplicate rows (same title, or same whitespace-normalised content) are skipped, both
  within the file and against the live knowledge base, so re-importing a file cannot
  crowd top_k with near-identical chunks. There is no force-import override — update
  the existing item in the admin UI instead;
- every row runs inside its own savepoint, so one bad row cannot invalidate the
  session and drag the rest of the batch down with it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import KnowledgeSource, KnowledgeStatus
from app.models.knowledge import KnowledgeItem
from app.services.knowledge import create_item

log = get_logger("import")

# Column limits from models/knowledge.py — pre-truncating keeps an over-long field from
# blowing up the row at the DB layer with an opaque error.
_TITLE_MAX = 500
_CATEGORY_MAX = 120

_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


# ----------------------------------------------------------------- decoding


def _decode(data: bytes) -> tuple[str, str, bool]:
    """bytes → (text, encoding, lossy).

    Strict UTF-8 must come first: GB18030 will happily "decode" valid UTF-8 bytes into
    mojibake, but the reverse direction fails fast, so the order makes detection safe.
    The errors="replace" fallback is a last resort and is flagged as lossy so the
    caller can warn instead of silently importing U+FFFD garbage.
    """
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc), enc, False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8", True


# ----------------------------------------------------------------- content cleanup


def _looks_like_html(text: str) -> bool:
    # Density check: a lone "<X>" in prose (model names, comparisons) is not markup;
    # two or more tags, or any closing tag, almost certainly is.
    tags = _TAG_RE.findall(text)
    return len(tags) >= 2 or any(t.startswith("</") for t in tags)


class _TextExtractor(HTMLParser):
    """Tag stripper: keeps text, drops script/style bodies, maps block boundaries to
    newlines so "<p>a</p><p>b</p>" doesn't fuse into "ab". Entities (&nbsp; etc.) are
    decoded by HTMLParser's convert_charrefs default."""

    _BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
        "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)


def _clean_content(content: str) -> str:
    """Strip rich-text markup from imported content before it reaches
    chunking/embedding/FTS; plain text and Markdown pass through untouched."""
    if not _looks_like_html(content):
        return content
    parser = _TextExtractor()
    parser.feed(content)
    parser.close()
    text = parser.text
    text = re.sub(r"[^\S\n]+", " ", text)  # collapse spaces/tabs/nbsp, keep newlines
    text = re.sub(r" ?\n ?", "\n", text)
    # Newlines here come from block tags, not prose — one is enough for the
    # sentence splitter, and blank lines would only inflate token counts.
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _content_fingerprint(content: str) -> str:
    """Hash over whitespace-normalised content, so a re-formatted copy still dedupes."""
    return hashlib.sha256(re.sub(r"\s+", "", content).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- parsing


def _norm_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.replace("；", ";").replace("，", ",").replace(";", ",").split(",") if t.strip()]
    return []


def _norm_row(title: str, content: str, category: str, tags) -> dict:
    return {
        "title": title.strip()[:_TITLE_MAX],
        "content": _clean_content(content.strip()),
        "category": category.strip()[:_CATEGORY_MAX],
        "tags": _norm_tags(tags),
    }


def parse_rows(text: str, fmt: str) -> list[dict]:
    rows: list[dict] = []
    if fmt == "json":
        payload = json.loads(text)
        items = payload if isinstance(payload, list) else payload.get("items", [])
        for it in items:
            rows.append(
                _norm_row(
                    str(it.get("title", "")),
                    str(it.get("content", "")),
                    str(it.get("category", "")),
                    it.get("tags", []),
                )
            )
    else:  # csv
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rows.append(
                _norm_row(
                    row.get("title") or row.get("标题") or "",
                    row.get("content") or row.get("内容") or "",
                    row.get("category") or row.get("分类") or "",
                    row.get("tags") or row.get("标签") or "",
                )
            )
    return rows


# ----------------------------------------------------------------- import


async def import_knowledge(db: AsyncSession, data: bytes, fmt: str, actor=None) -> dict:
    text, encoding, lossy = _decode(data)
    warnings: list[str] = []
    if lossy:
        ratio = text.count("�") / len(text) if text else 0.0
        warnings.append(
            f"文件编码无法识别，已按 UTF-8 宽松解码（替换符占比 {ratio:.1%}），"
            "导入内容可能损坏，建议转存为 UTF-8 后重试"
        )
        log.warning("import_lossy_decode", replacement_ratio=round(ratio, 4))

    base = {"skipped_duplicates": 0, "skipped": [], "encoding": encoding, "warnings": warnings}
    try:
        rows = parse_rows(text, fmt)
    except Exception as exc:  # noqa: BLE001
        return {"created": 0, "failed": 0, "errors": [f"解析失败: {exc}"], "total": 0, **base}

    # Dedup baseline: titles + normalised-content hashes of live items (RLS scopes the
    # query to the tenant). Archived items have no chunks and don't retrieve, so they
    # shouldn't block a re-import.
    existing = (
        await db.execute(
            select(KnowledgeItem.title, KnowledgeItem.content).where(
                KnowledgeItem.status != KnowledgeStatus.ARCHIVED
            )
        )
    ).all()
    db_titles = {t for t, _ in existing if t}
    db_hashes = {_content_fingerprint(c) for _, c in existing}
    batch_titles: dict[str, int] = {}  # title → first row that created it
    batch_hashes: dict[str, int] = {}

    created, errors, skipped = 0, [], []
    for i, row in enumerate(rows, start=1):
        if not row["content"]:
            errors.append(f"第{i}行：content 为空，已跳过")
            continue
        fp = _content_fingerprint(row["content"])
        if row["title"] and row["title"] in db_titles:
            skipped.append(f"第{i}行：标题与知识库已有条目重复，已跳过（如需更新请编辑原条目）")
            continue
        if fp in db_hashes:
            skipped.append(f"第{i}行：内容与知识库已有条目重复，已跳过")
            continue
        dup_at = batch_titles.get(row["title"]) if row["title"] else None
        if dup_at is None:
            dup_at = batch_hashes.get(fp)
        if dup_at is not None:
            skipped.append(f"第{i}行：与本文件第{dup_at}行重复，已跳过")
            continue
        try:
            # Savepoint per row: a DB error (over-long field, bad characters) rolls back
            # only this row instead of invalidating the whole session.
            async with db.begin_nested():
                await create_item(
                    db,
                    {**row, "status": KnowledgeStatus.PUBLISHED, "source": KnowledgeSource.IMPORT},
                    actor,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"第{i}行：{exc}")
            continue
        created += 1
        if row["title"]:
            batch_titles[row["title"]] = i
        batch_hashes[fp] = i

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # Report the failure per-row style instead of surfacing a bare 500 — the admin
        # still gets the full row report, just with nothing persisted.
        await db.rollback()
        log.error("import_commit_failed", error=str(exc))
        errors.insert(0, f"提交失败，本批 {created} 条成功行未入库: {exc}")
        created = 0

    log.info(
        "import_done", created=created, failed=len(errors),
        skipped=len(skipped), encoding=encoding,
    )
    return {
        "created": created,
        "failed": len(errors),
        "errors": errors[:50],
        "total": len(rows),
        **base,
        "skipped_duplicates": len(skipped),
        "skipped": skipped[:50],
    }
