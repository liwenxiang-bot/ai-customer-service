"""Unit: batch import hygiene — encoding detection, header aliases, HTML stripping,
dedup, validity columns, dry-run, row isolation."""

from datetime import UTC, datetime

import pytest

from app.models.knowledge import KnowledgeItem, KnowledgeVersion
from app.services.import_knowledge import (
    _clean_content,
    _content_fingerprint,
    _decode,
    import_knowledge,
    parse_rows,
)


@pytest.fixture(autouse=True)
def no_enqueue(monkeypatch):
    """create_item enqueues an embed job (Redis); tests must stay offline."""

    async def _noop(db, item_id):
        return None

    monkeypatch.setattr("app.services.knowledge._enqueue_embed", _noop)


# ----------------------------------------------------------------- fakes


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSavepoint:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        self._mark = len(self._session.added)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # Roll back to savepoint: discard objects added by this row only.
            del self._session.added[self._mark:]
            self._session.savepoint_rollbacks += 1
        return False


class FakeSession:
    """Just enough AsyncSession surface for import_knowledge + create_item."""

    def __init__(self, existing=(), fail_titles=(), commit_error=None):
        self.existing = list(existing)  # (title, content) rows for the dedup baseline
        self.fail_titles = set(fail_titles)
        self.commit_error = commit_error
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.savepoint_rollbacks = 0

    async def execute(self, *args, **kwargs):
        return FakeResult(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        item = next((o for o in reversed(self.added) if isinstance(o, KnowledgeItem)), None)
        if item is not None and item.title in self.fail_titles:
            raise RuntimeError("value too long for type character varying(500)")

    def begin_nested(self):
        return _FakeSavepoint(self)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.added.clear()


def _item_titles(session):
    return [o.title for o in session.added if isinstance(o, KnowledgeItem)]


# ----------------------------------------------------------------- decoding (F31)


def test_decode_utf8_bom():
    text, enc, lossy = _decode("title,content\n退货,七天无理由\n".encode("utf-8-sig"))
    assert enc == "utf-8-sig"
    assert lossy is False
    assert text.startswith("title")  # BOM stripped


def test_decode_gb18030():
    raw = "title,content\n退货政策,支持7天无理由退货\n".encode("gb18030")
    text, enc, lossy = _decode(raw)
    assert enc == "gb18030"
    assert lossy is False
    assert "退货政策" in text


def test_decode_lossy_fallback():
    # Invalid in both UTF-8 and GB18030 (0xFF is never a valid GB18030 lead byte).
    text, enc, lossy = _decode(b"abc\xff\xffdef")
    assert enc == "utf-8"
    assert lossy is True
    assert "�" in text


# ----------------------------------------------------------------- cleaning (F32)


def test_html_content_is_stripped():
    rows, _warnings = parse_rows(
        "title,content\nt,<p>七天内可退货</p><p>需保留&nbsp;原始包装</p>\n", "csv"
    )
    assert rows[0]["content"] == "七天内可退货\n需保留 原始包装"


def test_html_script_and_attrs_dropped():
    html = '<div style="color:red">正文内容</div><script>alert(1)</script><div>第二段</div>'
    cleaned = _clean_content(html)
    assert "正文内容" in cleaned and "第二段" in cleaned
    assert "alert" not in cleaned and "style" not in cleaned


def test_markdown_kept_verbatim():
    md = "# 退货政策\n\n- 七天无理由\n- 保留包装\n\n| 字段 | 值 |"
    assert _clean_content(md) == md


def test_lone_angle_bracket_not_treated_as_html():
    text = "温度 <30 度时保存，型号<A>请注意防潮"
    assert _clean_content(text) == text


# ----------------------------------------------------------------- fingerprint (F29)


def test_fingerprint_ignores_whitespace():
    assert _content_fingerprint("支持 7 天\n无理由退货") == _content_fingerprint("支持7天无理由退货")
    assert _content_fingerprint("支持7天") != _content_fingerprint("支持8天")


# ----------------------------------------------------------------- import flow


CSV = (
    "title,content,category,tags\n"
    "退货政策,支持7天无理由退货,售后,退货;售后\n"
    "发货时间,付款后48小时内发货,物流,\n"
)


async def test_import_ok_reports_encoding():
    db = FakeSession()
    result = await import_knowledge(db, CSV.encode("utf-8"), "csv")
    assert result["created"] == 2
    assert result["failed"] == 0
    assert result["skipped_duplicates"] == 0
    assert result["encoding"] == "utf-8-sig"
    assert result["warnings"] == []
    assert db.commits == 1
    assert _item_titles(db) == ["退货政策", "发货时间"]


async def test_import_gb18030_file():
    db = FakeSession()
    result = await import_knowledge(db, CSV.encode("gb18030"), "csv")
    assert result["created"] == 2
    assert result["encoding"] == "gb18030"
    assert _item_titles(db) == ["退货政策", "发货时间"]


async def test_lossy_decode_warns():
    db = FakeSession()
    result = await import_knowledge(db, b"\xff\xfe\xff", "csv")
    assert result["encoding"] == "utf-8"
    assert result["warnings"] and "宽松解码" in result["warnings"][0]


async def test_dedup_against_db_by_title_and_content():
    db = FakeSession(
        existing=[
            ("退货政策", "旧版内容"),  # blocks row 1 by exact title
            ("其他标题", "付款后 48 小时内发货"),  # blocks row 2 by normalised content
        ]
    )
    result = await import_knowledge(db, CSV.encode("utf-8"), "csv")
    assert result["created"] == 0
    assert result["skipped_duplicates"] == 2
    assert "标题与知识库已有条目重复" in result["skipped"][0]
    assert "内容与知识库已有条目重复" in result["skipped"][1]
    assert _item_titles(db) == []


async def test_dedup_within_batch():
    csv_data = (
        "title,content\n"
        "退货政策,支持7天无理由退货\n"
        "退货政策,支持7天无理由退货\n"  # duplicate of row 1
        "别名条目,支持 7 天无理由退货\n"  # same content module whitespace
    )
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    assert result["skipped_duplicates"] == 2
    assert all("与本文件第1行重复" in msg for msg in result["skipped"])


async def test_row_failure_isolated_by_savepoint():
    csv_data = "title,content\n好行一,内容一\n坏行,内容二\n好行二,内容三\n"
    db = FakeSession(fail_titles={"坏行"})
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 2
    assert result["failed"] == 1
    assert "第2行" in result["errors"][0]
    assert db.savepoint_rollbacks == 1
    # The failed row's objects were rolled back; the good rows survived to commit.
    assert _item_titles(db) == ["好行一", "好行二"]
    assert db.commits == 1


async def test_commit_failure_reported_not_raised():
    db = FakeSession(commit_error=RuntimeError("connection lost"))
    result = await import_knowledge(db, CSV.encode("utf-8"), "csv")
    assert result["created"] == 0
    assert "提交失败" in result["errors"][0]
    assert db.rollbacks == 1


async def test_empty_content_still_reported():
    db = FakeSession()
    result = await import_knowledge(db, "title,content\n只有标题,\n".encode(), "csv")
    assert result["created"] == 0
    assert result["failed"] == 1
    assert "content 为空" in result["errors"][0]


async def test_json_import_cleans_and_dedupes():
    payload = {
        "items": [
            {"title": "A", "content": "<p>正文&amp;内容</p><p>第二段</p>", "tags": ["a", "b"]},
            {"title": "B", "content": "正文&内容\n第二段"},  # dup of A after cleaning
        ]
    }
    import json as _json

    db = FakeSession()
    result = await import_knowledge(db, _json.dumps(payload).encode("utf-8"), "json")
    assert result["created"] == 1
    assert result["skipped_duplicates"] == 1
    items = [o for o in db.added if isinstance(o, KnowledgeItem)]
    assert items[0].content == "正文&内容\n第二段"


# ----------------------------------------------------------------- header aliases (K27)


async def test_qa_headers_map_to_title_content():
    csv_data = "question,answer\n如何退货,七天内可申请退货\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    item = next(o for o in db.added if isinstance(o, KnowledgeItem))
    assert item.title == "如何退货"
    assert item.content == "七天内可申请退货"


async def test_chinese_qa_headers_with_padding_and_case():
    # Headers survive strip+lower normalisation; 问题/答案 map onto title/content.
    csv_data = " 问题 , 答案 ,Category\n运费谁承担,质量问题商家承担运费,售后\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    item = next(o for o in db.added if isinstance(o, KnowledgeItem))
    assert item.title == "运费谁承担"
    assert item.content == "质量问题商家承担运费"
    assert item.category == "售后"


async def test_unrecognized_headers_fail_with_single_error():
    csv_data = "id,name\n1,甲\n2,乙\n3,丙\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 0
    assert result["total"] == 0
    assert len(result["errors"]) == 1  # one targeted error, not one per row
    assert "未识别的列名" in result["errors"][0]
    assert "支持的表头" in result["errors"][0]
    assert db.added == []


async def test_unknown_extra_column_warns_but_imports():
    csv_data = "title,content,priority\nA,内容A,high\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    assert any("priority" in w for w in result["warnings"])


# ----------------------------------------------------------------- validity columns (K29)


async def test_validity_columns_imported_and_snapshotted():
    csv_data = (
        "title,content,effective_from,expires_at\n"
        "618大促,活动期间全场9折,2026-06-01,2026-06-20T23:59:59+08:00\n"
    )
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    item = next(o for o in db.added if isinstance(o, KnowledgeItem))
    assert item.effective_from == datetime(2026, 6, 1, tzinfo=UTC)  # naive input → UTC
    assert item.expires_at is not None and item.expires_at.tzinfo is not None
    # The version snapshot records status + window (migration 0016).
    ver = next(o for o in db.added if isinstance(o, KnowledgeVersion))
    assert ver.status == "published"
    assert ver.effective_from == item.effective_from
    assert ver.expires_at == item.expires_at


async def test_bad_validity_date_fails_that_row_only():
    csv_data = (
        "title,content,expires_at\n"
        "好行,内容一,2026-12-31\n"
        "坏行,内容二,2026/06/31\n"
    )
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    assert result["failed"] == 1
    assert "第2行" in result["errors"][0] and "无法解析" in result["errors"][0]
    assert result["failed_rows"][0]["raw"]["expires_at"] == "2026/06/31"


async def test_inverted_validity_window_fails_row():
    csv_data = "title,content,effective_from,expires_at\nA,内容A,2026-06-20,2026-06-01\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 0
    assert "失效时间必须晚于生效时间" in result["errors"][0]


# ----------------------------------------------------------------- dry run (K54)


async def test_dry_run_previews_without_writing():
    db = FakeSession(existing=[("退货政策", "旧版内容")])  # row 1 dupes the DB by title
    result = await import_knowledge(db, CSV.encode("utf-8"), "csv", dry_run=True)
    assert result["dry_run"] is True
    assert result["would_create"] == 1
    assert result["would_skip"] == 1
    assert result["encoding"] == "utf-8-sig"
    assert len(result["preview"]) == 2
    assert result["preview"][0]["title"] == "退货政策"
    assert db.added == []  # nothing persisted, nothing committed
    assert db.commits == 0


async def test_dry_run_reports_parse_failure():
    db = FakeSession()
    result = await import_knowledge(db, b"id,name\n1,x\n", "csv", dry_run=True)
    assert result["dry_run"] is True
    assert result["would_create"] == 0
    assert "未识别的列名" in result["errors"][0]
    assert result["preview"] == []
    assert db.commits == 0


# ----------------------------------------------------------------- failed rows (K28)


async def test_status_column_round_trip():
    # /knowledge/export emits a status column; re-importing it must respect draft,
    # default blanks to published, and reject junk with a per-row error.
    csv_data = (
        "title,content,status\n"
        "草稿条目,内容一,draft\n"
        "坏状态,内容二,publish\n"
        "默认条目,内容三,\n"
    )
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 2
    assert result["failed"] == 1
    assert "状态「publish」无效" in result["errors"][0]
    statuses = {o.title: o.status for o in db.added if isinstance(o, KnowledgeItem)}
    assert statuses["草稿条目"] == "draft"
    assert statuses["默认条目"] == "published"


async def test_failed_rows_echo_raw_values_for_reimport():
    csv_data = "title,content\n好行,内容一\n坏行,\n"
    db = FakeSession()
    result = await import_knowledge(db, csv_data.encode("utf-8"), "csv")
    assert result["created"] == 1
    assert result["failed"] == 1
    assert result["failed_rows"] == [
        {
            "row_no": 2,
            "raw": {
                "title": "坏行", "content": "", "category": "", "tags": "",
                "status": "", "effective_from": "", "expires_at": "",
            },
            "reason": "content 为空，已跳过",
        }
    ]
