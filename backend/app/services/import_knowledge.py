"""Batch knowledge import (CSV / JSON).

Parsing + item insert is fast and synchronous; the slow part (embedding) is enqueued
per item, so importing hundreds of rows never blocks the request (requirements §7, §13).
Returns per-row success/failure so the admin UI can show what went wrong.
"""

from __future__ import annotations

import csv
import io
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import KnowledgeSource, KnowledgeStatus
from app.services.knowledge import create_item

log = get_logger("import")


def _norm_tags(value) -> list[str]:
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.replace("；", ";").replace("，", ",").replace(";", ",").split(",") if t.strip()]
    return []


def parse_rows(data: bytes, fmt: str) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    rows: list[dict] = []
    if fmt == "json":
        payload = json.loads(text)
        items = payload if isinstance(payload, list) else payload.get("items", [])
        for it in items:
            rows.append(
                {
                    "title": str(it.get("title", "")).strip(),
                    "content": str(it.get("content", "")).strip(),
                    "category": str(it.get("category", "")).strip(),
                    "tags": _norm_tags(it.get("tags", [])),
                }
            )
    else:  # csv
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            rows.append(
                {
                    "title": (row.get("title") or row.get("标题") or "").strip(),
                    "content": (row.get("content") or row.get("内容") or "").strip(),
                    "category": (row.get("category") or row.get("分类") or "").strip(),
                    "tags": _norm_tags(row.get("tags") or row.get("标签") or ""),
                }
            )
    return rows


async def import_knowledge(db: AsyncSession, data: bytes, fmt: str, actor=None) -> dict:
    try:
        rows = parse_rows(data, fmt)
    except Exception as exc:  # noqa: BLE001
        return {"created": 0, "failed": 0, "errors": [f"解析失败: {exc}"], "total": 0}

    created, errors = 0, []
    for i, row in enumerate(rows, start=1):
        if not row["content"]:
            errors.append(f"第{i}行：content 为空，已跳过")
            continue
        try:
            await create_item(
                db,
                {**row, "status": KnowledgeStatus.PUBLISHED, "source": KnowledgeSource.IMPORT},
                actor,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"第{i}行：{exc}")
    await db.commit()
    log.info("import_done", created=created, failed=len(errors))
    return {"created": created, "failed": len(errors), "errors": errors[:50], "total": len(rows)}
