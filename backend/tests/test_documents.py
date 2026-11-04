"""Unit: document → Markdown conversion, heading sectioning, URL fetch/crawl guards."""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from app.rag import documents as D
from app.rag.documents import (
    Section,
    UnsupportedDocument,
    crawl,
    document_to_markdown,
    fetch_url,
    html_to_markdown,
    split_sections,
)

# ----------------------------------------------------------------- sectioning


def test_split_empty_and_short_single_section():
    assert split_sections("") == []
    out = split_sections("# 标题\n\n一段很短的正文。", "x.md")
    assert out == [Section("标题", "一段很短的正文。")]


def test_split_no_headings_uses_doc_title():
    out = split_sections("纯文本，没有任何标题。" * 200, "手册")
    assert len(out) == 1 and out[0].title == "手册"


def _doc(n_sections=4, body_len=600):
    parts = ["# 使用手册", "", "简介：这是一份手册。", ""]
    for i in range(n_sections):
        parts += [f"## 第{i}节", "", ("第" + str(i) + "节内容。") * (body_len // 6), ""]
    return "\n".join(parts)


def test_split_at_h2_with_doc_title_prefix_and_intro_carry():
    out = split_sections(_doc())
    assert [s.title for s in out] == [f"使用手册：第{i}节" for i in range(4)]
    for s in out:
        assert s.content.startswith("简介：这是一份手册。")  # short intro carried into every section
        assert "## 第" in s.content


def test_split_level_is_shallowest_repeated_level():
    md = "# 文档\n\n## 唯一的二级\n\n" + "\n\n".join(
        f"### 三级{i}\n\n" + "内容。" * 300 for i in range(3)
    )
    out = split_sections(md)
    # H2 occurs once, H3 three times → split at H3; the H2 heading has no body of its
    # own and is kept as the parent in the title path.
    assert [s.title for s in out] == [f"文档：唯一的二级：三级{i}" for i in range(3)]


def test_split_heading_without_body_is_not_a_section():
    md = "# 文档\n\n## 空小节\n\n## 有内容\n\n" + "内容。" * 600
    out = split_sections(md)
    assert [s.title for s in out] == ["文档：有内容"]


def test_split_ignores_headings_inside_code_fences():
    md = "# 文档\n\n## A\n\n" + "a。" * 500 + "\n\n```\n## 不是标题\n```\n\n## B\n\n" + "b。" * 500
    out = split_sections(md)
    assert [s.title for s in out] == ["文档：A", "文档：B"]
    assert "## 不是标题" in out[0].content


def test_split_long_intro_becomes_its_own_section():
    md = "# 文档\n\n" + "开场白。" * 200 + "\n\n## A\n\n" + "a。" * 500 + "\n\n## B\n\n" + "b。" * 500
    out = split_sections(md)
    assert out[0].title == "文档" and out[0].content.startswith("开场白")
    assert not out[1].content.startswith("开场白")  # too long to carry


def test_split_oversized_section_recurses_into_subheadings():
    md = "# 文档\n\n## A\n\n" + "\n\n".join(f"### A{i}\n\n" + "内容。" * 1200 for i in range(3)) + "\n\n## B\n\n" + "b。" * 300
    out = split_sections(md)
    titles = [s.title for s in out]
    assert "文档：B" in titles
    assert any(t.startswith("文档：A：A") for t in titles)
    assert all(s.chars <= D.HARD_MAX for s in out)


# ----------------------------------------------------------------- HTML


_PAGE = """<!doctype html><html><head><title>常见问题 – 示例文档</title></head><body>
<nav><a href="/">首页</a><a href="/a">导航</a></nav>
<main>
<div class="nextra-breadcrumb">文档 › 常见问题</div>
<h1>常见问题<a href="#x" class="subheading-anchor"></a></h1>
<p>遇到问题时<strong>先检查</strong>配置。</p>
<h2>Base URL 怎么填</h2>
<ol><li>填 <code>https://api.example.com/v1</code></li><li>保存</li></ol>
<table><tr><th>客户端</th><th>地址</th></tr><tr><td>Codex</td><td>/v1</td></tr></table>
<pre><code class="language-bash">curl -s https://api.example.com/v1/models</code></pre>
<button>Copy</button>
</main>
<footer>© 2026</footer><script>alert(1)</script></body></html>"""


def test_html_to_markdown_keeps_structure_drops_boilerplate():
    md, title = html_to_markdown(_PAGE)
    assert title == "常见问题"
    assert "导航" not in md and "© 2026" not in md and "alert" not in md and "›" not in md
    assert "# 常见问题" in md and "## Base URL 怎么填" in md
    assert "**先检查**" in md
    assert "1. 填 `https://api.example.com/v1`" in md
    assert "| 客户端 | 地址 |" in md and "| Codex | /v1 |" in md
    assert "```bash\ncurl -s" in md
    assert "Copy" not in md


def test_html_sections_end_to_end():
    md, title = html_to_markdown(_PAGE)
    out = split_sections(md, title, section_max=10)
    assert [s.title for s in out] == ["常见问题：Base URL 怎么填"]
    assert "| Codex | /v1 |" in out[0].content


# ----------------------------------------------------------------- conversion dispatch


def test_document_to_markdown_rejects_unknown_ext():
    with pytest.raises(UnsupportedDocument):
        document_to_markdown(b"x", "a.xlsx")
    with pytest.raises(UnsupportedDocument):
        document_to_markdown(b"x", "noext")


def test_markdown_and_txt_pass_through_with_gb18030():
    conv = document_to_markdown("# 标题\n\n正文".encode("gb18030"), "a.md")
    assert conv.markdown == "# 标题\n\n正文" and not conv.warnings
    conv = document_to_markdown(b"\xef\xbb\xbfplain", "a.txt")
    assert conv.markdown == "plain"


def _docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """Minimal .docx: [(style_id, text)]; style ids Heading1/Heading2 map to headings."""
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        if style else f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        for style, text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr("word/document.xml", document)
    return buf.getvalue()


def test_docx_to_markdown_headings_and_sections():
    data = _docx([
        ("Heading1", "售后手册"),
        ("", "总则。"),
        ("Heading2", "退货"),
        ("", "七天无理由。" * 200),
        ("Heading2", "换货"),
        ("", "十五天换货。" * 200),
    ])
    conv = document_to_markdown(data, "手册.docx")
    assert "# 售后手册" in conv.markdown and "## 退货" in conv.markdown
    out = split_sections(conv.markdown, "手册")
    assert [s.title for s in out] == ["售后手册：退货", "售后手册：换货"]
    assert out[0].content.startswith("总则。")


def test_docx_rejects_non_zip():
    with pytest.raises(UnsupportedDocument):
        document_to_markdown(b"not a zip", "a.docx")


def _pdf(pages: list[str]) -> bytes:
    """Hand-built single-font PDF with one text line per page (pypdf extracts it)."""
    objs: list[bytes] = []
    n_pages = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    font_id = 3 + 2 * n_pages
    for i, text in enumerate(pages):
        page_id = 3 + 2 * i
        content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        objs.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_pdf_to_markdown_pages_become_sections():
    conv = document_to_markdown(_pdf(["Return policy: seven days", "Exchange policy: fifteen days"]), "policy.pdf")
    assert "## 第 1 页" in conv.markdown and "seven days" in conv.markdown
    assert "## 第 2 页" in conv.markdown and "fifteen days" in conv.markdown
    assert conv.warnings  # "按页切分" notice
    out = split_sections(conv.markdown, "policy", section_max=10)
    assert [s.title for s in out] == ["policy：第 1 页", "policy：第 2 页"]


def test_pdf_without_text_layer_is_rejected():
    with pytest.raises(UnsupportedDocument, match="扫描件"):
        document_to_markdown(_pdf([" "]), "scan.pdf")


# ----------------------------------------------------------------- URL fetch / crawl


def _site(pages: dict[str, tuple[str, str]]):
    """MockTransport over {path: (content_type, body)}."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path
        if key in pages:
            ctype, body = pages[key]
            return httpx.Response(200, headers={"content-type": ctype}, content=body.encode("utf-8"))
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_url_rejects_unsafe_and_unsupported(monkeypatch):
    with pytest.raises(UnsupportedDocument):
        await fetch_url("ftp://example.com/a")
    client = httpx.AsyncClient(transport=_site({"/img": ("image/png", "x")}))
    with pytest.raises(UnsupportedDocument, match="内容类型"):
        await fetch_url("https://example.com/img", client)
    with pytest.raises(UnsupportedDocument, match="HTTP 404"):
        await fetch_url("https://example.com/missing", client)
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_url_html_and_markdown_sniffing():
    client = httpx.AsyncClient(transport=_site({
        "/doc": ("text/html; charset=utf-8", "<html><title>T</title><body><main><h1>T</h1><p>hi</p></main></body></html>"),
        "/readme.md": ("application/octet-stream", "# R\n\nbody"),
    }))
    f = await fetch_url("example.com/doc", client)  # scheme defaulted to https
    assert f.ext == ".html" and f.url == "https://example.com/doc"
    f = await fetch_url("https://example.com/readme.md", client)
    assert f.ext == ".md"  # extension wins when the server lies about the type
    await client.aclose()


@pytest.mark.asyncio
async def test_crawl_stays_under_prefix_and_same_site(monkeypatch):
    html = lambda title, links: (  # noqa: E731
        "text/html",
        f"<html><title>{title}</title><body><main><h1>{title}</h1><p>{title} body</p>"
        + "".join(f'<a href="{href}">l</a>' for href in links) + "</main></body></html>",
    )
    pages = {
        "/docs/": html("Home", ["/docs/a", "/docs/b#frag", "/docs/b?x=1", "/other", "https://evil.example/x", "/docs/img.png"]),
        "/docs/a": html("A", ["/docs/"]),
        "/docs/b": html("B", ["/docs/c"]),
        "/docs/c": html("C", []),
        "/other": html("Other", []),
        "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /docs/c\n"),
    }
    transport = _site(pages)
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(D.httpx, "AsyncClient", patched)
    fetched, warnings = await crawl("https://example.com/docs/", max_pages=10)
    urls = [f.url for f in fetched]
    assert urls[0] == "https://example.com/docs/"
    assert set(urls) == {"https://example.com/docs/", "https://example.com/docs/a", "https://example.com/docs/b"}
    assert any("robots.txt" in w for w in warnings)  # /docs/c disallowed


@pytest.mark.asyncio
async def test_crawl_page_cap_reports_remaining(monkeypatch):
    links = "".join(f'<a href="/d/{j}">x</a>' for j in range(6))
    pages = {f"/d/{i}": ("text/html", f"<html><title>P{i}</title><body><main><p>p{i}</p>{links}</main></body></html>") for i in range(6)}
    pages["/d/"] = ("text/html", f"<html><title>Index</title><body><main><p>index</p>{links}</main></body></html>")
    transport = _site(pages)
    real = httpx.AsyncClient
    monkeypatch.setattr(D.httpx, "AsyncClient", lambda *a, **k: real(*a, **{**k, "transport": transport}))
    fetched, warnings = await crawl("https://example.com/d/", max_pages=3)
    assert len(fetched) == 3
    assert any("上限" in w for w in warnings)
