"""Batch knowledge import (CSV / JSON rows, uploaded documents, URLs).

Three entry points share one insert loop (`_import_rows`: dedup → savepoint-per-row
insert → report):
- `import_knowledge`: CSV / JSON, one item per row;
- `import_document`: md / txt / html / docx / pdf, converted to Markdown and cut into
  one item per heading section (see rag.documents);
- `import_url`: one page, or a bounded same-site crawl, sectioned the same way. Items
  remember their source URL, so importing the same URL again UPDATES changed sections
  (new version) instead of skipping them as duplicates — that is how a docs site is
  kept in sync.

Parsing + item insert is fast and synchronous; the slow part (embedding) is enqueued
per item, so importing hundreds of rows never blocks the request (requirements §7, §13).
Returns per-row success/failure so the admin UI can show what went wrong; failed rows
are echoed back verbatim (failed_rows) so the UI can offer a fix-and-reimport file.

Import hygiene matters more here than in the editor, because a bad batch silently
poisons retrieval for a long time:
- bytes are decoded strictly (UTF-8 first, then GB18030 — Excel's default CSV encoding
  on Chinese Windows) so mojibake never reaches the embedding index unnoticed;
- headers are normalised (strip+lower) and aliased — Q&A exports (question/answer,
  问题/答案) map onto title/content; a file whose header carries no recognisable
  content column fails with ONE targeted error instead of a per-row cascade;
- HTML-looking content is stripped to plain text before storage (CMS exports carry
  <p>/<div>/&nbsp; noise into vectors and FTS tokens); Markdown is kept verbatim —
  it is the editor's designed storage format and embeddings are robust to it;
- duplicate rows (same title, or same whitespace-normalised content) are skipped, both
  within the file and against the live knowledge base, so re-importing a file cannot
  crowd top_k with near-identical chunks. There is no force-import override — update
  the existing item in the admin UI instead;
- every row runs inside its own savepoint, so one bad row cannot invalidate the
  session and drag the rest of the batch down with it;
- dry_run performs the full parse/clean/dedup pass without touching the DB, so a
  batch can be sanity-checked before it lands.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import KnowledgeSource, KnowledgeStatus
from app.models.knowledge import KnowledgeItem
from app.rag.documents import (
    Section,
    UnsupportedDocument,
    crawl,
    document_to_markdown,
    fetch_url,
    leading_title,
    split_sections,
)
from app.services.knowledge import create_item, update_item

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

# Header/key aliases, matched after strip+lower. question/answer → title/content is the
# right semantic mapping for FAQ exports: the question is the retrieval anchor (titles
# are joined into the embedding input at chunking time).
_COLUMN_ALIASES = {
    "title": "title", "标题": "title", "question": "title", "问题": "title",
    "content": "content", "内容": "content", "answer": "content", "答案": "content", "回答": "content",
    "category": "category", "分类": "category",
    "tags": "tags", "标签": "tags",
    "effective_from": "effective_from", "生效时间": "effective_from",
    "expires_at": "expires_at", "失效时间": "expires_at", "过期时间": "expires_at",
    # status closes the export→re-import round trip (/knowledge/export emits it).
    "status": "status", "状态": "status",
}
_SUPPORTED_HEADERS_HINT = (
    "title/标题/question/问题、content/内容/answer/答案、category/分类、tags/标签、"
    "status/状态、effective_from/生效时间、expires_at/失效时间"
)
_VALID_STATUSES = {"draft", "published", "archived"}


def _norm_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.replace("；", ";").replace("，", ",").replace(";", ",").split(",") if t.strip()]
    return []


def _parse_window_dt(value) -> datetime | None:
    """ISO-8601 date or datetime; naive values are taken as UTC (same convention as the
    admin API's _coerce_utc). Raises ValueError so the row gets a targeted error."""
    raw = str(value or "").strip()
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _norm_row(title: str, content: str, category: str, tags,
              effective_from=None, expires_at=None, status=None, *,
              clean_html: bool = True, extra: dict | None = None) -> dict:
    row = {
        "title": title.strip()[:_TITLE_MAX],
        # Document/URL rows are already Markdown (converted from HTML upstream); the
        # HTML sniff would only mangle legitimate inline tags in them.
        "content": _clean_content(content.strip()) if clean_html else content.strip(),
        "category": category.strip()[:_CATEGORY_MAX],
        "tags": _norm_tags(tags),
        "status": str(status or "").strip().lower() or None,  # None → importer default
        "extra": dict(extra or {}),
        "effective_from": None,
        "expires_at": None,
        # Row-level parse problem (bad date, inverted window) — reported per row later.
        "error": "",
        # Original values, echoed back on failure so the UI can rebuild an importable CSV.
        "raw": {
            "title": title, "content": content, "category": category,
            "tags": tags if isinstance(tags, str) else ",".join(str(t) for t in (tags or [])),
            "status": str(status or ""),
            "effective_from": str(effective_from or ""),
            "expires_at": str(expires_at or ""),
        },
    }
    if row["status"] and row["status"] not in _VALID_STATUSES:
        row["error"] = f"状态「{status}」无效，仅支持 draft/published/archived（留空默认 published）"
    for src, field, label in (
        (effective_from, "effective_from", "生效时间"),
        (expires_at, "expires_at", "失效时间"),
    ):
        try:
            row[field] = _parse_window_dt(src)
        except (ValueError, TypeError):
            row["error"] = f"{label}「{src}」无法解析，请使用 ISO 日期（如 2026-01-31 或 2026-01-31 08:00:00）"
    if not row["error"] and row["effective_from"] and row["expires_at"] \
            and row["expires_at"] <= row["effective_from"]:
        row["error"] = "失效时间必须晚于生效时间"
    return row


def _map_keys(raw: dict) -> dict:
    """Original dict → {canonical_field: value}; first alias hit wins."""
    canon: dict = {}
    for k, v in raw.items():
        field = _COLUMN_ALIASES.get(str(k or "").strip().lower())
        if field and field not in canon:
            canon[field] = v
    return canon


def parse_rows(text: str, fmt: str) -> tuple[list[dict], list[str]]:
    """→ (rows, warnings). Raises ValueError with an operator-facing message when no
    content column can be recognised — one clear error beats "content 为空" × N rows."""
    rows: list[dict] = []
    warnings: list[str] = []
    if fmt == "json":
        payload = json.loads(text)
        items = payload if isinstance(payload, list) else payload.get("items", [])
        mapped = [_map_keys(it) for it in items]
        if mapped and all("content" not in m for m in mapped):
            raise ValueError(f"未识别的列名：所有条目都缺少内容字段；支持的字段名：{_SUPPORTED_HEADERS_HINT}")
        for m in mapped:
            rows.append(
                _norm_row(
                    str(m.get("title", "")),
                    str(m.get("content", "")),
                    str(m.get("category", "")),
                    m.get("tags", []),
                    m.get("effective_from"),
                    m.get("expires_at"),
                    m.get("status"),
                )
            )
    else:  # csv
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
        header_map: dict[str, str] = {}  # original header → canonical field
        unknown: list[str] = []
        for name in fieldnames:
            field = _COLUMN_ALIASES.get((name or "").strip().lower())
            if field and field not in header_map.values():
                header_map[name] = field
            elif (name or "").strip():
                unknown.append((name or "").strip())
        if fieldnames and "content" not in header_map.values():
            raise ValueError(
                f"未识别的列名：{'、'.join(unknown) or '（空表头）'}；支持的表头：{_SUPPORTED_HEADERS_HINT}"
            )
        if unknown:
            # DictReader would drop these silently — an admin adding an unsupported
            # column must not believe it was imported.
            warnings.append(f"以下列未被识别，已忽略：{'、'.join(unknown)}；支持的表头：{_SUPPORTED_HEADERS_HINT}")
        for row in reader:
            m = {field: (row.get(name) or "") for name, field in header_map.items()}
            rows.append(
                _norm_row(
                    m.get("title", ""),
                    m.get("content", ""),
                    m.get("category", ""),
                    m.get("tags", ""),
                    m.get("effective_from") or None,
                    m.get("expires_at") or None,
                    m.get("status") or None,
                )
            )
    return rows, warnings


# ----------------------------------------------------------------- import

_FAILED_ROWS_MAX = 200  # verbatim failed rows echoed back for fix-and-reimport

_ROW_FIELDS = ("title", "content", "category", "tags", "effective_from", "expires_at")


def _preview_row(row: dict) -> dict:
    return {
        **{k: row[k] for k in ("title", "content", "category", "tags")},
        "effective_from": row["effective_from"].isoformat() if row["effective_from"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "error": row["error"] or None,
    }


def _failure(base: dict, msg: str, dry_run: bool) -> dict:
    failure = {"errors": [msg], "total": 0, "failed_rows": [], **base}
    if dry_run:
        return {"dry_run": True, "would_create": 0, "would_skip": 0, "preview": [], **failure}
    return {"created": 0, "failed": 0, "updated": 0, **failure}


async def _import_rows(
    db: AsyncSession,
    rows: list[dict],
    *,
    base: dict,
    source: str,
    actor=None,
    dry_run: bool = False,
    upsert_by_source: bool = False,
) -> dict:
    """Shared insert loop: dedup against the KB and within the batch, savepoint per row,
    one report dict for both dry-run and real runs.

    `upsert_by_source`: rows whose extra.source_url + title match an existing item
    update that item when the content changed (and count as `updated`) instead of
    being skipped as a title duplicate — the re-sync path for URL imports."""
    encoding = base.get("encoding", "")
    # Dedup baseline: titles + normalised-content hashes of live items (RLS scopes the
    # query to the tenant). Archived items have no chunks and don't retrieve, so they
    # shouldn't block a re-import.
    existing = (
        await db.execute(
            select(KnowledgeItem.id, KnowledgeItem.title, KnowledgeItem.content, KnowledgeItem.extra)
            .where(KnowledgeItem.status != KnowledgeStatus.ARCHIVED)
        )
    ).all()
    db_titles = {r[1] for r in existing if r[1]}
    db_hashes = {_content_fingerprint(r[2]) for r in existing}
    # (source_url, title) → (item_id, content hash) for the upsert path
    by_source: dict[tuple[str, str], tuple] = {}
    if upsert_by_source:
        for r in existing:
            src = (r[3] or {}).get("source_url") if isinstance(r[3], dict) else None
            if src and r[1]:
                by_source[(src, r[1])] = (r[0], _content_fingerprint(r[2]))
    batch_titles: dict[str, int] = {}  # title → first row that created it
    batch_hashes: dict[str, int] = {}

    created, updated, errors, skipped = 0, 0, [], []
    failed_rows: list[dict] = []

    def _fail(i: int, row: dict, reason: str) -> None:
        errors.append(f"第{i}行：{reason}")
        if len(failed_rows) < _FAILED_ROWS_MAX:
            failed_rows.append({"row_no": i, "raw": row["raw"], "reason": reason})

    for i, row in enumerate(rows, start=1):
        if row["error"]:
            _fail(i, row, f"{row['error']}，已跳过")
            continue
        if not row["content"]:
            _fail(i, row, "content 为空，已跳过")
            continue
        fp = _content_fingerprint(row["content"])
        src_key = (row["extra"].get("source_url", ""), row["title"]) if upsert_by_source else None
        if src_key and src_key[0] and src_key in by_source:
            item_id, old_fp = by_source[src_key]
            if old_fp == fp:
                skipped.append(f"第{i}行：「{row['title']}」内容未变化，已跳过")
                continue
            if not dry_run:
                try:
                    async with db.begin_nested():
                        item = await db.get(KnowledgeItem, item_id)
                        if item is None:
                            raise RuntimeError("原条目不存在")
                        await update_item(
                            db, item,
                            {"content": row["content"], "category": row["category"] or item.category},
                            actor,
                        )
                except Exception as exc:  # noqa: BLE001
                    _fail(i, row, str(exc))
                    continue
            updated += 1
            batch_titles[row["title"]] = i
            batch_hashes[fp] = i
            continue
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
        if not dry_run:
            try:
                # Savepoint per row: a DB error (over-long field, bad characters) rolls
                # back only this row instead of invalidating the whole session.
                async with db.begin_nested():
                    await create_item(
                        db,
                        {
                            **{k: row[k] for k in _ROW_FIELDS},
                            "status": row["status"] or KnowledgeStatus.PUBLISHED,
                            "source": source,
                            "extra": row["extra"],
                        },
                        actor,
                    )
            except Exception as exc:  # noqa: BLE001
                _fail(i, row, str(exc))
                continue
        created += 1
        if row["title"]:
            batch_titles[row["title"]] = i
        batch_hashes[fp] = i

    if dry_run:
        log.info(
            "import_dry_run", would_create=created, would_update=updated, failed=len(errors),
            skipped=len(skipped), encoding=encoding, source=source,
        )
        return {
            "dry_run": True,
            "would_create": created,
            "would_update": updated,
            "would_skip": len(skipped),
            "errors": errors[:50],
            "total": len(rows),
            **base,
            "skipped_duplicates": len(skipped),
            "skipped": skipped[:50],
            "failed_rows": failed_rows,
            "preview": [_preview_row(r) for r in rows[:10]],
        }

    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        # Report the failure per-row style instead of surfacing a bare 500 — the admin
        # still gets the full row report, just with nothing persisted.
        await db.rollback()
        log.error("import_commit_failed", error=str(exc))
        errors.insert(0, f"提交失败，本批 {created + updated} 条成功行未入库: {exc}")
        created = updated = 0

    log.info(
        "import_done", created=created, updated=updated, failed=len(errors),
        skipped=len(skipped), encoding=encoding, source=source,
    )
    return {
        "created": created,
        "updated": updated,
        "failed": len(errors),
        "errors": errors[:50],
        "total": len(rows),
        **base,
        "skipped_duplicates": len(skipped),
        "skipped": skipped[:50],
        "failed_rows": failed_rows,
    }


async def import_knowledge(
    db: AsyncSession, data: bytes, fmt: str, actor=None, dry_run: bool = False
) -> dict:
    """CSV / JSON rows → items (one per row)."""
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
        rows, parse_warnings = parse_rows(text, fmt)
        warnings.extend(parse_warnings)
    except Exception as exc:  # noqa: BLE001
        return _failure(base, f"解析失败: {exc}", dry_run)
    return await _import_rows(
        db, rows, base=base, source=KnowledgeSource.IMPORT, actor=actor, dry_run=dry_run
    )


# ----------------------------------------------------------------- documents / URLs

_SECTIONS_PREVIEW_MAX = 300


def _section_rows(
    sections: list[Section], *, category: str, status: str | None, tags: list[str],
    extra: dict, clean_html: bool = False,
) -> list[dict]:
    rows = []
    for s in sections:
        rows.append(
            _norm_row(
                s.title, s.content, category, tags, status=status,
                clean_html=clean_html, extra={**extra, "section": s.title},
            )
        )
    return rows


def _sections_report(rows: list[dict]) -> list[dict]:
    return [
        {"title": r["title"], "chars": len(r["content"]), "error": r["error"] or None}
        for r in rows[:_SECTIONS_PREVIEW_MAX]
    ]


async def import_document(
    db: AsyncSession, data: bytes, filename: str, *, actor=None, dry_run: bool = False,
    category: str = "", status: str | None = None, tags: list[str] | None = None,
) -> dict:
    """One uploaded document (md / txt / html / docx / pdf) → one item per heading section."""
    base: dict = {"skipped_duplicates": 0, "skipped": [], "encoding": "", "warnings": []}
    try:
        conv = document_to_markdown(data, filename)
    except UnsupportedDocument as exc:
        return _failure(base, str(exc), dry_run)
    except Exception as exc:  # noqa: BLE001
        log.warning("document_convert_failed", filename=filename, error=str(exc))
        return _failure(base, f"文件解析失败：{type(exc).__name__}", dry_run)
    base["warnings"].extend(conv.warnings)
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename or "").strip()
    doc_title = leading_title(conv.markdown, conv.title or stem)
    sections = split_sections(conv.markdown, conv.title or stem)
    if not sections:
        return _failure(base, "文档没有可导入的文字内容", dry_run)
    rows = _section_rows(
        sections, category=category, status=status, tags=tags or [],
        extra={"source_kind": "document", "source_name": filename or ""},
        # .md / .txt uploads never went through an HTML converter; an HTML-looking
        # snippet in them is still worth stripping, Markdown passes untouched.
        clean_html=(filename or "").lower().endswith((".md", ".markdown", ".mdx", ".txt")),
    )
    base["doc_title"] = doc_title
    base["sections"] = _sections_report(rows)
    return await _import_rows(
        db, rows, base=base, source=KnowledgeSource.DOCUMENT, actor=actor, dry_run=dry_run
    )


async def import_url(
    db: AsyncSession, url: str, *, crawl_site: bool = False, max_pages: int = 50,
    actor=None, dry_run: bool = False, category: str = "", status: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """One page, or a same-site crawl under its path, → sectioned items that remember
    their source URL (re-import updates changed sections)."""
    base: dict = {"skipped_duplicates": 0, "skipped": [], "encoding": "", "warnings": []}
    try:
        if crawl_site:
            pages, warnings = await crawl(url, max_pages=max_pages)
            base["warnings"].extend(warnings)
        else:
            pages = [await fetch_url(url)]
    except UnsupportedDocument as exc:
        return _failure(base, str(exc), dry_run)
    except Exception as exc:  # noqa: BLE001
        log.warning("url_fetch_failed", url=url, error=str(exc))
        return _failure(base, f"抓取失败：{type(exc).__name__}", dry_run)
    if not pages:
        return _failure(base, "没有抓到任何页面", dry_run)

    rows: list[dict] = []
    page_report: list[dict] = []
    for page in pages:
        try:
            conv = document_to_markdown(page.data, f"page{page.ext}")
        except UnsupportedDocument as exc:
            base["warnings"].append(f"{page.url}：{exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            base["warnings"].append(f"{page.url}：解析失败（{type(exc).__name__}）")
            continue
        slug = re.sub(r"[-_/]+", " ", page.url.rstrip("/").rsplit("/", 1)[-1]).strip() or page.url
        page_title = leading_title(conv.markdown, conv.title or slug)
        sections = split_sections(conv.markdown, conv.title or slug)
        page_report.append({"url": page.url, "title": page_title, "sections": len(sections)})
        rows.extend(
            _section_rows(
                sections, category=category, status=status, tags=tags or [],
                extra={"source_kind": "url", "source_url": page.url, "source_name": page_title},
            )
        )
    if not rows:
        return _failure(base, "抓到的页面没有可导入的文字内容", dry_run)
    base["pages"] = page_report
    base["sections"] = _sections_report(rows)
    return await _import_rows(
        db, rows, base=base, source=KnowledgeSource.URL, actor=actor, dry_run=dry_run,
        upsert_by_source=True,
    )
