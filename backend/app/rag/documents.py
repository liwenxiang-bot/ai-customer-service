"""Documents → Markdown → knowledge sections (file upload and URL import).

The knowledge base stores Markdown. Everything an operator uploads is normalised to
Markdown first, then cut into knowledge items at heading boundaries:

- Markdown / TXT are taken as-is (strict UTF-8 → GB18030 decoding, see import_knowledge).
- HTML goes through BeautifulSoup + markdownify: boilerplate (nav, header, footer,
  aside, script, breadcrumbs, heading permalinks) is dropped, `<main>`/`<article>` is
  preferred over `<body>`, tables and fenced code survive.
- DOCX goes through mammoth (headings, lists, tables → Markdown).
- PDF goes through pypdf text extraction. PDFs carry no heading structure, so a PDF
  becomes one item per page-group; scanned PDFs (no text layer) are rejected with a
  clear message — there is no OCR here.

Sectioning (`split_sections`) is what makes a 40-page manual retrievable: each
section becomes its own item titled "文档标题：小节标题", so the title anchor the
chunker joins into every embedding (see services.knowledge.reembed_item) is the
section heading, not the document name. The split level is chosen per document — the
shallowest heading level that occurs at least twice — and short intro text before the
first heading is carried into every section as context.

URL import reuses the same path: fetch (SSRF-checked, size- and time-capped) → sniff
type → convert → split. `crawl` collects same-site pages under the start URL's path
prefix (breadth-first, bounded), which is how a docs site comes in as one batch.
"""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.core.logging import get_logger
from app.core.net import UnsafeURL, check_outbound_url

log = get_logger("rag.documents")

SUPPORTED_EXTS: tuple[str, ...] = (
    ".md", ".markdown", ".mdx", ".txt", ".html", ".htm", ".docx", ".pdf",
)

# Sectioning knobs. A document at or under SECTION_MAX chars stays one item (one FAQ
# answer, one short page); above it we split at headings. HARD_MAX is when a section
# is split again at the next heading level.
SECTION_MAX = 1500
HARD_MAX = 6000
INTRO_CARRY_MAX = 300  # intro text shorter than this is prepended to every section

# URL fetch limits
FETCH_MAX_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 20.0
CRAWL_MAX_PAGES = 50
CRAWL_TIME_BUDGET = 45.0
_UA = "acs-knowledge-importer/1.0 (+knowledge import; respects robots.txt)"

_MIME_EXT = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/markdown": ".md",
    "text/x-markdown": ".md",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


class UnsupportedDocument(ValueError):
    """Operator-facing: the file/URL cannot be turned into text."""


@dataclass
class Section:
    title: str
    content: str

    @property
    def chars(self) -> int:
        return len(self.content)


@dataclass
class Converted:
    markdown: str
    title: str  # document title hint ("" when the source carries none)
    warnings: list[str]


# ----------------------------------------------------------------- conversion


def _decode_text(data: bytes) -> tuple[str, bool]:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc), False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), True


_BOILERPLATE_TAGS = (
    "script", "style", "noscript", "template", "svg", "button", "form", "iframe",
    "nav", "header", "footer", "aside",
)
_BOILERPLATE_CLASS = re.compile(
    r"breadcrumb|sidebar|toc\b|table-of-contents|pagination|nextra-nav|skip-nav|sr-only|"
    r"subheading-anchor|edit-link|feedback|cookie|banner|advert",
    re.I,
)


def html_to_markdown(html: str) -> tuple[str, str]:
    """→ (markdown, <title>). Boilerplate is removed before conversion so navigation
    menus and footers never become knowledge."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
    # Site titles are usually "页面 – 站点名" / "页面 | 站点名": keep the page part.
    title = re.split(r"\s+[–—|·-]\s+", title)[0].strip() if title else ""

    root = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in root.find_all(_BOILERPLATE_TAGS):
        tag.decompose()
    for tag in root.find_all(attrs={"class": _BOILERPLATE_CLASS}):
        tag.decompose()
    for tag in root.find_all(attrs={"role": re.compile(r"navigation|banner|contentinfo", re.I)}):
        tag.decompose()
    for tag in root.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()

    def _lang(el):
        # markdownify hands us the <pre>; highlighters put the class on <pre> or <code>.
        candidates = [el] + [c for c in el.find_all("code", limit=1)]
        for node in candidates:
            for cls in node.get("class") or []:
                if cls.startswith(("language-", "lang-")):
                    return cls.split("-", 1)[1]
        return None

    md = markdownify(
        str(root), heading_style="ATX", bullets="-", strip=["img"], code_language_callback=_lang,
    )
    return _tidy_markdown(md), title


def _tidy_markdown(md: str) -> str:
    md = md.replace("\r\n", "\n")
    md = re.sub(r"\[\]\([^)]*\)", "", md)  # empty-text links (permalink anchors)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    # Nextra-style "Copy" buttons and similar single-word leftovers next to code blocks
    md = re.sub(r"\n(Copy|复制)\n", "\n", md)
    return md.strip()


_FRONTMATTER = re.compile(r"\A﻿?---[ \t]*\n(.*?)\n---[ \t]*\n", re.S)
_JSX_IMPORT = re.compile(r"^\s*(import|export)\s.*$")
_JSX_SELF_CLOSING = re.compile(r"^\s*<([A-Za-z][\w.]*)\b([^>]*)/>\s*$")
_JSX_OPEN = re.compile(r"^\s*<([A-Z][\w.]*)\b[^>]*>\s*$")
_JSX_CLOSE = re.compile(r"^\s*</([A-Z][\w.]*)>\s*$")
_JSX_INLINE_CALLOUT = re.compile(r"^\s*<Callout\b[^>]*>(.*?)</Callout>\s*$")
_JSX_ATTR = re.compile(r'(\w+)=(?:"([^"]*)"|\'([^\']*)\'|\{`([^`]*)`\})')
_HTML_ANCHOR = re.compile(r"<a\s+id=[\"'][^\"']*[\"']\s*/?>(</a>)?")
_HTML_INLINE = (
    (re.compile(r"<(strong|b)>(.*?)</\1>", re.S), r"**\2**"),
    (re.compile(r"<code>(.*?)</code>", re.S), r"`\1`"),
    (re.compile(r"<br\s*/?>"), "\n"),
)


def mdx_to_markdown(text: str) -> tuple[str, str]:
    """MDX (Nextra / Docusaurus pages) → plain Markdown, plus the frontmatter title.

    Drops import/export lines and component tags, turns <Callout> into a blockquote,
    keeps an image component's caption (it is usually the instruction the screenshot
    illustrates) and leaves fenced code untouched."""
    title = ""
    m = _FRONTMATTER.match(text)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "title":
                title = value.strip().strip("'\"")
        text = text[m.end():]
    out: list[str] = []
    fence: str | None = None
    quote_depth = 0
    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        fm = _FENCE.match(s)
        if fm:
            if fence is None:
                fence = fm.group(1)
            elif s.startswith(fence):
                fence = None
            out.append(raw)
            continue
        if fence is not None:
            out.append(raw)
            continue
        if _JSX_IMPORT.match(raw):
            continue
        im = _JSX_INLINE_CALLOUT.match(raw)
        if im:
            out.extend(["", f"> {im.group(1).strip()}", ""])
            continue
        sm = _JSX_SELF_CLOSING.match(raw)
        if sm:
            attrs = {k: (a or b or c) for k, a, b, c in _JSX_ATTR.findall(sm.group(2))}
            caption = (attrs.get("caption") or "").strip()
            if caption and sm.group(1)[0].isupper():
                out.extend(["", caption, ""])
            continue  # anchors (<a id=… />), images, other leaf components
        om = _JSX_OPEN.match(raw)
        if om:
            if om.group(1) == "Callout":
                quote_depth += 1
                out.append("")
            continue
        cm = _JSX_CLOSE.match(raw)
        if cm:
            if cm.group(1) == "Callout" and quote_depth:
                quote_depth -= 1
                out.append("")
            continue
        line = _HTML_ANCHOR.sub("", raw)
        for pat, repl in _HTML_INLINE:
            line = pat.sub(repl, line)
        if quote_depth:
            line = ("> " + line.strip()) if line.strip() else ">"
        out.append(line)
    return _tidy_markdown("\n".join(out)), title


def _docx_to_markdown(data: bytes) -> Converted:
    import mammoth

    result = mammoth.convert_to_markdown(io.BytesIO(data))
    warnings = sorted({m.message for m in result.messages if m.type == "warning"})[:5]
    md = _tidy_markdown(result.value or "")
    # mammoth emits setext-style "Title\n===" for Heading 1; normalise to ATX so the
    # sectioner sees one heading syntax.
    md = re.sub(r"^(.+)\n=+\n", r"# \1\n", md, flags=re.M)
    md = re.sub(r"^(.+)\n-{3,}\n", r"## \1\n", md, flags=re.M)
    return Converted(md, "", [f"Word 转换提示：{w}" for w in warnings])


def _pdf_to_markdown(data: bytes) -> Converted:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedDocument("PDF 已加密，请先解除密码再上传") from exc
    pages: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("pdf_page_extract_failed", page=i, error=str(exc))
            text = ""
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        if text:
            pages.append(f"## 第 {i} 页\n\n{text}")
    body = "\n\n".join(pages)
    if len(re.sub(r"\s+", "", body)) < 20:
        raise UnsupportedDocument("PDF 没有可提取的文字层（可能是扫描件），暂不支持 OCR，请提供文字版")
    meta_title = ""
    try:
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:  # noqa: BLE001
        meta_title = ""
    warnings = ["PDF 没有标题结构，已按页切分；表格和多栏排版可能错位，建议导入后检查"]
    return Converted(_tidy_markdown(body), meta_title, warnings)


def document_to_markdown(data: bytes, filename: str) -> Converted:
    """Any supported upload → Markdown. Raises UnsupportedDocument for the rest."""
    ext = PurePosixPath((filename or "").lower()).suffix
    if ext not in SUPPORTED_EXTS:
        raise UnsupportedDocument(
            f"不支持的文件类型「{ext or '无扩展名'}」，支持：{', '.join(SUPPORTED_EXTS)}"
        )
    if ext == ".pdf":
        return _pdf_to_markdown(data)
    if ext == ".docx":
        if data[:4] != b"PK\x03\x04":
            raise UnsupportedDocument("不是有效的 .docx 文件（旧版 .doc 请先另存为 .docx）")
        return _docx_to_markdown(data)
    text, lossy = _decode_text(data)
    warnings = ["文件编码无法识别，已按 UTF-8 宽松解码，内容可能损坏"] if lossy else []
    if ext in (".html", ".htm"):
        md, title = html_to_markdown(text)
        return Converted(md, title, warnings)
    if ext == ".mdx":
        md, title = mdx_to_markdown(text)
        return Converted(md, title, warnings)
    return Converted(_tidy_markdown(text), "", warnings)


# ----------------------------------------------------------------- sectioning

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^(```|~~~)")


@dataclass
class _Heading:
    level: int
    text: str
    line: int


def _headings(lines: list[str]) -> list[_Heading]:
    out: list[_Heading] = []
    fence: str | None = None
    for i, raw in enumerate(lines):
        s = raw.strip()
        m = _FENCE.match(s)
        if m:
            if fence is None:
                fence = m.group(1)
            elif s.startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        h = _HEADING.match(s)
        if h:
            out.append(_Heading(len(h.group(1)), h.group(2).strip(), i))
    return out


def _clean_title(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links → text
    text = re.sub(r"[`*_]+", "", text)
    return text.strip()


def split_sections(markdown: str, doc_title: str = "", *, section_max: int = SECTION_MAX,
                   hard_max: int = HARD_MAX) -> list[Section]:
    """Cut a Markdown document into knowledge-item-sized sections at heading boundaries.

    Returns [] for an empty document. A document with no headings, or one that fits in
    `section_max`, is a single section titled `doc_title` (or its first H1)."""
    md = (markdown or "").strip()
    if not md:
        return []
    lines = md.split("\n")
    heads = _headings(lines)

    # Document title: a leading H1 (before any other content) names the document and
    # is not a section of its own.
    if heads and heads[0].level == 1 and not "".join(lines[: heads[0].line]).strip():
        doc_title = _clean_title(heads[0].text) or doc_title
        lines = lines[heads[0].line + 1:]
        heads = _headings(lines)
    doc_title = (doc_title or "").strip()
    body = "\n".join(lines).strip()
    if not body:
        return []

    if len(body) <= section_max or not heads:
        return [Section(doc_title or _fallback_title(body), body)]

    # Split level: shallowest level that occurs at least twice; else the shallowest.
    counts: dict[int, int] = {}
    for h in heads:
        counts[h.level] = counts.get(h.level, 0) + 1
    repeated = [lv for lv, n in counts.items() if n >= 2]
    level = min(repeated) if repeated else min(counts)
    boundaries = [h for h in heads if h.level <= level]
    if not boundaries:
        return [Section(doc_title or _fallback_title(body), body)]

    intro = "\n".join(lines[: boundaries[0].line]).strip()
    carry = intro if 0 < len(intro) <= INTRO_CARRY_MAX else ""
    out: list[Section] = []
    if intro and not carry:
        # A long intro is a section in its own right rather than silently dropped.
        out.append(Section(doc_title or _fallback_title(intro), intro))

    parents: dict[int, str] = {}  # level → current heading text, for shallower parents
    for idx, h in enumerate(boundaries):
        end = boundaries[idx + 1].line if idx + 1 < len(boundaries) else len(lines)
        chunk = "\n".join(lines[h.line:end]).strip()
        body_only = "\n".join(lines[h.line + 1:end]).strip()
        parents = {lv: t for lv, t in parents.items() if lv < h.level}
        parents[h.level] = _clean_title(h.text)
        if not body_only:
            continue  # heading with nothing under it (its children are their own sections)
        path = [t for _, t in sorted(parents.items())]
        title = "：".join(p for p in ([doc_title] if doc_title else []) + path if p)
        content = f"{carry}\n\n{chunk}" if carry else chunk
        if len(content) > hard_max and any(x.level > h.level for x in _headings(lines[h.line:end])):
            out.extend(split_sections(chunk, title, section_max=section_max, hard_max=hard_max))
        else:
            out.append(Section(title[:500], content))
    return out or [Section(doc_title or _fallback_title(body), body)]


def leading_title(markdown: str, fallback: str = "") -> str:
    """The document's own title: a leading H1 (same rule split_sections applies), else
    `fallback` — used for reports so "手册.md" shows as its real heading."""
    lines = (markdown or "").strip().split("\n")
    heads = _headings(lines)
    if heads and heads[0].level == 1 and not "".join(lines[: heads[0].line]).strip():
        return _clean_title(heads[0].text) or fallback
    return fallback


def _fallback_title(text: str) -> str:
    first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
    return _clean_title(first.lstrip("#").strip())[:80] or "未命名文档"


# ----------------------------------------------------------------- URL fetch / crawl


@dataclass
class Fetched:
    url: str
    data: bytes
    ext: str  # normalised extension for document_to_markdown


def _ext_for(url: str, content_type: str) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    suffix = PurePosixPath(urlparse(url).path.lower()).suffix
    return suffix if suffix in SUPPORTED_EXTS else ""


def _assert_safe(url: str) -> None:
    try:
        check_outbound_url(url)
    except UnsafeURL as exc:
        raise UnsupportedDocument(str(exc)) from exc


async def fetch_url(url: str, client: httpx.AsyncClient | None = None) -> Fetched:
    """GET with SSRF check (also on the redirect target), size cap and timeout."""
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    _assert_safe(url)
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0), follow_redirects=True,
        headers={"User-Agent": _UA, "Accept": "text/html,text/markdown,text/plain,application/pdf,*/*;q=0.5"},
    )
    try:
        async with client.stream("GET", url) as resp:
            final = str(resp.url)
            if final != url:
                _assert_safe(final)
            if resp.status_code >= 400:
                raise UnsupportedDocument(f"抓取失败：HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "")
            ext = _ext_for(final, ctype)
            if not ext:
                raise UnsupportedDocument(f"不支持的内容类型「{ctype.split(';')[0] or '未知'}」")
            buf = bytearray()
            async for part in resp.aiter_bytes():
                buf.extend(part)
                if len(buf) > FETCH_MAX_BYTES:
                    raise UnsupportedDocument("页面超过 8MB 上限")
            return Fetched(final, bytes(buf), ext)
    except httpx.HTTPError as exc:
        raise UnsupportedDocument(f"抓取失败：{type(exc).__name__}") from exc
    finally:
        if own:
            await client.aclose()


def _same_site_links(base_url: str, html: str, prefix: str) -> list[str]:
    from bs4 import BeautifulSoup

    base = urlparse(base_url)
    out: list[str] = []
    seen: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base_url, a["href"]))[0]
        p = urlparse(href)
        if p.scheme not in ("http", "https") or p.netloc != base.netloc:
            continue
        if not p.path.startswith(prefix):
            continue
        if PurePosixPath(p.path.lower()).suffix not in ("", ".html", ".htm", ".md"):
            continue
        href = href.split("?")[0].rstrip("/") or href
        if href not in seen:
            seen.add(href)
            out.append(href)
    return out


async def _robots(client: httpx.AsyncClient, start: str) -> RobotFileParser | None:
    p = urlparse(start)
    rp = RobotFileParser()
    try:
        resp = await client.get(f"{p.scheme}://{p.netloc}/robots.txt")
        if resp.status_code != 200:
            return None
        rp.parse(resp.text.splitlines())
        return rp
    except httpx.HTTPError:
        return None


async def crawl(start_url: str, max_pages: int = CRAWL_MAX_PAGES) -> tuple[list[Fetched], list[str]]:
    """Breadth-first same-site crawl under the start URL's path prefix.

    → (pages, warnings). The start page is always first. Bounded by `max_pages` and
    CRAWL_TIME_BUDGET; robots.txt disallow rules are honoured when present."""
    max_pages = max(1, min(int(max_pages), CRAWL_MAX_PAGES))
    start_url = (start_url or "").strip()
    if not start_url.lower().startswith(("http://", "https://")):
        start_url = "https://" + start_url
    sp = urlparse(start_url)
    prefix = sp.path if sp.path.endswith("/") else sp.path.rsplit("/", 1)[0] + "/"
    if PurePosixPath(sp.path).suffix == "":
        prefix = sp.path.rstrip("/") + "/" if sp.path.strip("/") else "/"
    warnings: list[str] = []
    pages: list[Fetched] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CRAWL_TIME_BUDGET
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0), follow_redirects=True,
        headers={"User-Agent": _UA},
    ) as client:
        rp = await _robots(client, start_url)
        queue: list[str] = [start_url]
        seen: set[str] = {start_url.rstrip("/")}
        while queue and len(pages) < max_pages:
            if loop.time() > deadline:
                warnings.append(f"抓取超时，已停止在 {len(pages)} 页（剩余 {len(queue)} 个链接未抓）")
                break
            url = queue.pop(0)
            if rp is not None and not rp.can_fetch(_UA, url):
                warnings.append(f"robots.txt 禁止抓取，已跳过：{url}")
                continue
            try:
                f = await fetch_url(url, client)
            except UnsupportedDocument as exc:
                warnings.append(f"{url}：{exc}")
                continue
            pages.append(f)
            if f.ext == ".html":
                for link in _same_site_links(f.url, f.data.decode("utf-8", errors="replace"), prefix):
                    key = link.rstrip("/")
                    if key not in seen:
                        seen.add(key)
                        queue.append(link)
        if queue and len(pages) >= max_pages:
            warnings.append(f"已达到 {max_pages} 页上限，还有 {len(queue)} 个链接未抓；可从未抓的目录再导入一次")
    return pages, warnings
