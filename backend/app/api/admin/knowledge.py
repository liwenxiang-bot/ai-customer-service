"""Knowledge base admin: CRUD, versions/rollback, import/export, review queue,
test retrieval, usage stats, category rename, full rebuild."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_operator
from app.core.net import client_ip
from app.core.strings import escape_like
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.enums import KnowledgeSource, KnowledgeStatus, ReviewStatus
from app.models.knowledge import (
    EmbeddingRebuildJob,
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeReviewCandidate,
    KnowledgeVersion,
    RetrievalEvalCase,
)
from app.services import knowledge as ksvc
from app.services.ai_config import get_active_ai_config
from app.services.audit import write_audit
from app.services.embedding_rebuild import start_rebuild
from app.services.import_knowledge import import_knowledge
from app.services.knowledge_gaps import collect_gaps, ignore_question
from app.services.knowledge_questions import (
    add_question,
    generate_questions,
    items_missing_questions,
)
from app.services.retrieval_eval import add_case, list_cases

router = APIRouter(prefix="/knowledge", tags=["admin-knowledge"])

# Column limits mirror models/knowledge.py (String(500)/String(120)) and the import
# path's _TITLE_MAX/_CATEGORY_MAX — a readable 422 instead of a raw DB DataError.
_TITLE_MAX = 500
_CATEGORY_MAX = 120
_IMPORT_MAX_BYTES = 20 * 1024 * 1024  # matches deploy/nginx.conf client_max_body_size 20m

StatusLiteral = Literal["draft", "published", "archived"]


def _client_ip(request: Request) -> str:
    return client_ip(request)


_escape_like = escape_like  # shared with canned/conversations search (app.core.strings)


# ----------------------------------------------------------------- schemas
def _coerce_utc(v: datetime | None) -> datetime | None:
    """Naive input is taken as UTC so window comparisons and timestamptz storage are
    unambiguous regardless of the caller's serializer."""
    return v if v is None or v.tzinfo else v.replace(tzinfo=UTC)


class ItemIn(BaseModel):
    title: str = Field("", max_length=_TITLE_MAX)
    content: str
    category: str = Field("", max_length=_CATEGORY_MAX)
    tags: list[str] = []
    # Customer phrasings (rag.questions); each becomes a question chunk on embed.
    questions: list[str] = Field(default_factory=list, max_length=30)
    status: StatusLiteral = "published"
    # Validity window: NULL = unbounded on that side; retrieval skips items outside it.
    effective_from: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("effective_from", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return _coerce_utc(v)

    @model_validator(mode="after")
    def _window_ordered(self):
        if self.effective_from and self.expires_at and self.expires_at <= self.effective_from:
            raise ValueError("expires_at 必须晚于 effective_from")
        return self


class ItemUpdate(BaseModel):
    title: str | None = Field(None, max_length=_TITLE_MAX)
    content: str | None = None
    category: str | None = Field(None, max_length=_CATEGORY_MAX)
    tags: list[str] | None = None
    questions: list[str] | None = Field(None, max_length=30)
    questions_auto: list[str] | None = Field(None, max_length=30)  # editor removes suggestions
    status: StatusLiteral | None = None
    # None clears the bound when the field is explicitly sent (checked via fields_set).
    effective_from: datetime | None = None
    expires_at: datetime | None = None
    change_note: str = "edited"
    # Optimistic lock: the version the client loaded before editing. When sent and it no
    # longer matches, the save is rejected (409) instead of silently overwriting a
    # concurrent edit.
    base_version: int | None = None

    @field_validator("effective_from", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return _coerce_utc(v)


def _item_dict(
    it: KnowledgeItem, chunk_count: int | None = None, ready_count: int | None = None
) -> dict:
    return {
        "id": str(it.id),
        "title": it.title,
        "content": it.content,
        "category": it.category,
        "tags": it.tags,
        "questions": list(it.questions or []),
        "questions_auto": list(it.questions_auto or []),
        "status": it.status,
        "source": it.source,
        "version": it.version,
        "chunk_count": chunk_count,
        "ready_count": ready_count,  # embedded chunks; < chunk_count ⇒ vectors missing
        "effective_from": it.effective_from.isoformat() if it.effective_from else None,
        "expires_at": it.expires_at.isoformat() if it.expires_at else None,
        "extra": it.extra or {},  # provenance (e.g. source session/message for一键入库)
        "created_at": it.created_at.isoformat() if it.created_at else None,
        "updated_at": it.updated_at.isoformat() if it.updated_at else None,
    }


# ----------------------------------------------------------------- list / CRUD
def _apply_list_filters(
    stmt,
    *,
    q: str = "",
    category: str = "",
    status: str = "",
    source: str = "",
    tag: str = "",
    expired: bool | None = None,
    validity: str = "",
):
    """Shared by the list view and export so both see the exact same subset."""
    if q:
        like = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                KnowledgeItem.title.ilike(like, escape="\\"),
                KnowledgeItem.content.ilike(like, escape="\\"),
            )
        )
    if category:
        stmt = stmt.where(KnowledgeItem.category == category)
    if status:
        stmt = stmt.where(KnowledgeItem.status == status)
    if source:
        stmt = stmt.where(KnowledgeItem.source == source)
    if tag:
        stmt = stmt.where(KnowledgeItem.tags.contains([tag]))  # JSONB @> [tag]
    now = datetime.now(UTC)
    if expired is not None:
        if expired:
            stmt = stmt.where(KnowledgeItem.expires_at.is_not(None), KnowledgeItem.expires_at <= now)
        else:
            stmt = stmt.where(
                or_(KnowledgeItem.expires_at.is_(None), KnowledgeItem.expires_at > now)
            )
    if validity == "expired":
        stmt = stmt.where(KnowledgeItem.expires_at.is_not(None), KnowledgeItem.expires_at <= now)
    elif validity == "scheduled":  # 未生效：retrieval 同样会跳过它们
        stmt = stmt.where(
            KnowledgeItem.effective_from.is_not(None), KnowledgeItem.effective_from > now
        )
    elif validity == "active":
        stmt = stmt.where(
            or_(KnowledgeItem.effective_from.is_(None), KnowledgeItem.effective_from <= now),
            or_(KnowledgeItem.expires_at.is_(None), KnowledgeItem.expires_at > now),
        )
    return stmt


@router.get("")
async def list_items(
    q: str = "",
    category: str = "",
    status: str = "",
    source: str = "",
    tag: str = "",
    expired: bool | None = Query(None, description="true=仅已过期条目，false=排除已过期"),
    validity: Literal["", "expired", "scheduled", "active"] = Query(
        "", description="时效状态：expired=已过期 scheduled=未生效 active=生效中"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    stmt = _apply_list_filters(
        select(KnowledgeItem),
        q=q, category=category, status=status, source=source, tag=tag,
        expired=expired, validity=validity,
    )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            # id as tiebreaker: bulk imports/status flips share one transaction-level
            # updated_at, and unstable ordering makes offset pages repeat/drop rows.
            stmt.order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    coverage = await ksvc.chunk_coverage(db, [it.id for it in rows])
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _item_dict(it, *coverage.get(str(it.id), (0, 0))) for it in rows
        ],
    }


@router.get("/categories")
async def categories(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    rows = (
        await db.execute(
            select(KnowledgeItem.category, func.count())
            .where(KnowledgeItem.category != "")
            .group_by(KnowledgeItem.category)
        )
    ).all()
    return {"categories": [{"name": r[0], "count": r[1]} for r in rows]}


@router.get("/tags")
async def tags(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    rows = (
        await db.execute(
            text(
                "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag FROM knowledge_items "
                "WHERE jsonb_array_length(tags) > 0 ORDER BY tag"
            )
        )
    ).all()
    return {"tags": [r[0] for r in rows]}


# ------------------------------------------------- export / stats / bulk ops
# NOTE: single-segment routes here MUST be declared before GET /{item_id}, or the
# catch-all would swallow them (e.g. /export → item_id="export" → 404).


@router.get("/export")
async def export_items(
    format: Literal["csv", "json"] = Query("csv"),
    q: str = "",
    category: str = "",
    status: str = "",
    source: str = "",
    tag: str = "",
    validity: Literal["", "expired", "scheduled", "active"] = Query(""),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Export matching items for backup/migration/batch-edit. CSV columns mirror the
    import template so an exported file can be re-imported."""
    stmt = _apply_list_filters(
        select(KnowledgeItem),
        q=q, category=category, status=status, source=source, tag=tag, validity=validity,
    )
    rows = (
        await db.execute(
            stmt.order_by(KnowledgeItem.updated_at.desc(), KnowledgeItem.id.desc()).limit(10000)
        )
    ).scalars().all()
    stamp = datetime.now(UTC).strftime("%Y%m%d")

    if format == "json":
        payload = [
            {
                "id": str(it.id),
                "title": it.title,
                "content": it.content,
                "category": it.category,
                "tags": it.tags,
                "status": it.status,
                "source": it.source,
                "version": it.version,
                "effective_from": it.effective_from.isoformat() if it.effective_from else None,
                "expires_at": it.expires_at.isoformat() if it.expires_at else None,
                "updated_at": it.updated_at.isoformat() if it.updated_at else None,
            }
            for it in rows
        ]
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="knowledge_{stamp}.json"'},
        )

    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly
    w = csv.writer(buf)
    w.writerow(["title", "content", "category", "tags", "status", "effective_from", "expires_at"])
    for it in rows:
        w.writerow([
            it.title, it.content, it.category,
            ";".join(str(t) for t in (it.tags or [])),  # import splits on ; / ,
            it.status,
            it.effective_from.isoformat() if it.effective_from else "",
            it.expires_at.isoformat() if it.expires_at else "",
        ])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="knowledge_{stamp}.csv"'},
    )


@router.get("/usage-stats")
async def usage_stats(
    days: int = Query(90, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Per-item citation counts aggregated from message citations (same unnest as the
    dashboard's top-cited list, without its LIMIT 10) — surfaces dead/hot knowledge."""
    rows = (
        await db.execute(
            text(
                """
                SELECT c->>'item_id' AS item_id,
                       count(*) AS citation_count,
                       max(m.created_at) AS last_cited_at
                FROM messages m, jsonb_array_elements(m.citations) c
                WHERE m.created_at >= now() - make_interval(days => :days)
                  AND jsonb_array_length(m.citations) > 0
                  AND c->>'item_id' IS NOT NULL
                GROUP BY c->>'item_id'
                ORDER BY citation_count DESC
                LIMIT 1000
                """
            ),
            {"days": days},
        )
    ).all()
    return {
        "days": days,
        "items": [
            {
                "item_id": r[0],
                "citation_count": int(r[1]),
                "last_cited_at": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ],
    }


class CategoryRenameIn(BaseModel):
    from_name: str = Field(min_length=1, max_length=_CATEGORY_MAX)
    to_name: str = Field("", max_length=_CATEGORY_MAX)  # 空 = 取消分类


@router.post("/category/rename")
async def rename_category(
    body: CategoryRenameIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Rename/merge a category in one UPDATE (to_name 为已有分类即合并，为空即取消分类)。"""
    from_name, to_name = body.from_name.strip(), body.to_name.strip()
    if not from_name:
        raise HTTPException(status_code=400, detail="原分类名不能为空")
    if from_name == to_name:
        raise HTTPException(status_code=400, detail="新旧分类名相同，无需修改")
    res = await db.execute(
        update(KnowledgeItem)
        .where(KnowledgeItem.category == from_name)
        .values(category=to_name)
        .execution_options(synchronize_session=False)
    )
    updated = res.rowcount or 0
    if not updated:
        raise HTTPException(status_code=404, detail=f"没有分类为「{from_name}」的条目，请刷新分类列表")
    await write_audit(
        db, user, "knowledge.category_rename", "knowledge_item", "",
        {"from": from_name, "to": to_name, "updated": updated}, ip=_client_ip(request),
    )
    await db.commit()
    return {"updated": updated}


@router.post("/rebuild", status_code=202)
async def rebuild_all(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Full re-chunk + re-embed with the current model/dim — the KB-page twin of
    POST /ai-config/rebuild (retry a failed rebuild, or apply new chunk params)."""
    cfg = await get_active_ai_config(db)
    job = await start_rebuild(
        db, cfg.embedding_model, cfg.embedding_dim, cfg.embedding_model, cfg.embedding_dim
    )
    await write_audit(
        db, user, "knowledge.rebuild", "embedding_rebuild_job", str(job.id),
        {"model": cfg.embedding_model, "dim": cfg.embedding_dim}, ip=_client_ip(request),
    )
    await db.commit()
    return {"job_id": str(job.id), "status": job.status}


@router.get("/review/count")
async def review_count(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    """Lightweight badge count — the review list pulls full candidate bodies."""
    pending = (
        await db.execute(
            select(func.count(KnowledgeReviewCandidate.id)).where(
                KnowledgeReviewCandidate.status == ReviewStatus.PENDING
            )
        )
    ).scalar_one()
    return {"pending": int(pending)}


@router.post("", status_code=201)
async def create_item(
    body: ItemIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    # The service dict only carries the classic fields; the validity window is set on
    # the flushed row here so the service API stays unchanged.
    item = await ksvc.create_item(db, body.model_dump(exclude={"effective_from", "expires_at"}), user)
    item.effective_from = body.effective_from
    item.expires_at = body.expires_at
    await db.commit()
    return _item_dict(item, 0)


async def _get_or_404(db: AsyncSession, item_id: str) -> KnowledgeItem:
    try:
        item = await db.get(KnowledgeItem, uuid.UUID(item_id))
    except (ValueError, TypeError):
        item = None
    if not item:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    return item


# ----------------------------------------------------------------- customer phrasings
class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=120)


@router.post("/questions/generate-missing", status_code=202)
async def generate_missing_questions(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)
):
    """Queue LLM question suggestions for every published item that has none (≤300)."""
    from app.db.tenant_context import get_current_tenant
    from app.tasks.queue import enqueue

    ids = await items_missing_questions(db)
    tenant = get_current_tenant()
    for iid in ids:
        await enqueue("suggest_questions", str(iid), _tenant=str(tenant) if tenant else None)
    await write_audit(db, user, "knowledge.questions_bulk", "knowledge_item", "", {"queued": len(ids)})
    await db.commit()
    return {"queued": len(ids)}


class DistillIn(BaseModel):
    message_id: str = Field(min_length=8, max_length=64)


@router.post("/distill", status_code=201)
async def distill_message(
    body: DistillIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    """Operator's "沉淀为知识": turn one (human or AI) answer + its question into a review
    candidate right away. 409 when nothing usable or already distilled."""
    from app.services.distill import distill_from_message

    try:
        uuid.UUID(body.message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id 无效")
    ok = await distill_from_message(db, body.message_id)
    if not ok:
        raise HTTPException(status_code=409, detail="这条回复已沉淀过，或没有可提炼的问答（需要前面有客户提问）")
    await write_audit(db, user, "knowledge.distill", "message", body.message_id, {})
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- eval cases (calibration)
class EvalCaseIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    expected_item_id: str | None = None  # None ⇒ the KB has no answer; retrieval should return nothing
    source: str = Field("manual", max_length=32)


@router.get("/eval-cases")
async def list_eval_cases(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    rows = await list_cases(db)
    return {
        "items": [
            {
                "id": str(r.id), "query": r.query,
                "expected_item_id": str(r.expected_item_id) if r.expected_item_id else None,
                "expected_title": r.expected_title, "source": r.source, "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/eval-cases", status_code=201)
async def create_eval_case(
    body: EvalCaseIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    title = ""
    if body.expected_item_id:
        item = await _get_or_404(db, body.expected_item_id)
        title = item.title
    row = await add_case(
        db, body.query, body.expected_item_id, title, body.source, getattr(user, "email", "") or ""
    )
    await db.commit()
    return {"ok": True, "id": str(row.id), "expected_title": title}


@router.delete("/eval-cases/{case_id}")
async def delete_eval_case(
    case_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="用例不存在")
    row = await db.get(RetrievalEvalCase, cid)
    if row is None:
        raise HTTPException(status_code=404, detail="用例不存在")
    await db.delete(row)
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- knowledge gaps
@router.get("/gaps")
async def knowledge_gaps(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Customer questions the knowledge base could not answer, grouped by meaning —
    the worklist for adding knowledge (see services.knowledge_gaps)."""
    return await collect_gaps(db, days)


class GapIgnoreIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


@router.post("/gaps/ignore", status_code=201)
async def knowledge_gap_ignore(
    body: GapIgnoreIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Dismiss a gap (and its rephrasings) from the list."""
    row = await ignore_question(db, body.question, getattr(user, "email", "") or "")
    await write_audit(
        db, user, "knowledge.gap_ignore", "knowledge_gap_ignore", str(row.id),
        {"question": body.question[:200]}, ip=_client_ip(request),
    )
    await db.commit()
    return {"ok": True, "id": str(row.id)}


@router.get("/{item_id}")
async def get_item(item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    item = await _get_or_404(db, item_id)
    chunk_count = (
        await db.execute(select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.item_id == item.id))
    ).scalar_one()
    return _item_dict(item, chunk_count)


@router.put("/{item_id}")
async def update_item(
    item_id: str, body: ItemUpdate, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    item = await _get_or_404(db, item_id)
    if body.base_version is not None and body.base_version != item.version:
        last = (
            await db.execute(
                select(KnowledgeVersion)
                .where(KnowledgeVersion.item_id == item.id)
                .order_by(KnowledgeVersion.version.desc())
                .limit(1)
            )
        ).scalars().first()
        editor = (last.editor_email if last else "") or "其他管理员"
        raise HTTPException(
            status_code=409,
            detail=(
                f"保存冲突：该条目已被 {editor} 修改（当前为 v{item.version}，"
                f"你基于 v{body.base_version} 编辑），请刷新后合并修改再保存"
            ),
        )
    # For the window fields None is a meaningful value ("clear the bound"), so apply
    # them only when explicitly sent, and validate against the merged final state.
    eff = body.effective_from if "effective_from" in body.model_fields_set else item.effective_from
    exp = body.expires_at if "expires_at" in body.model_fields_set else item.expires_at
    if eff and exp and exp <= eff:
        raise HTTPException(status_code=400, detail="expires_at 必须晚于 effective_from")
    data = {
        k: v
        for k, v in body.model_dump(exclude={"effective_from", "expires_at", "base_version"}).items()
        if v is not None or k == "change_note"
    }
    # Window fields go through the service so they land in the version snapshot and
    # the changed_fields audit entry (None = clear the bound, hence explicit-send only).
    if "effective_from" in body.model_fields_set:
        data["effective_from"] = eff
    if "expires_at" in body.model_fields_set:
        data["expires_at"] = exp
    await ksvc.update_item(db, item, data, user)
    await db.commit()
    return _item_dict(item)


@router.post("/{item_id}/questions/generate")
async def generate_item_questions(
    item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    """Replace the item's LLM-suggested phrasings (hand-written ones are untouched)."""
    item = await _get_or_404(db, item_id)
    try:
        generated = await generate_questions(db, item, user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"生成失败：{type(exc).__name__}（请检查 LLM 配置）")
    await db.commit()
    return {**_item_dict(item), "generated": generated}


@router.post("/{item_id}/questions")
async def add_item_question(
    item_id: str, body: QuestionIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    """Append one customer phrasing (the gap list's "归入已有知识")."""
    item = await _get_or_404(db, item_id)
    questions = await add_question(db, item, body.question, user)
    await db.commit()
    return {**_item_dict(item), "questions": questions}


@router.delete("/{item_id}")
async def delete_item(
    item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    item = await _get_or_404(db, item_id)
    # Deleting is hard (versions/chunks CASCADE): archiving first acts as the second
    # confirmation and keeps draft/archived cleanup friction-free.
    if item.status == KnowledgeStatus.PUBLISHED:
        raise HTTPException(
            status_code=409,
            detail="已发布条目不可直接删除：请先归档（归档后即停止参与检索），确认无影响后再删除",
        )
    await ksvc.delete_item(db, item, user)
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- versions
@router.get("/{item_id}/versions")
async def list_versions(
    item_id: str,
    full: bool = Query(False, description="true=返回完整正文（全文预览/逐行对比用）"),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    item = await _get_or_404(db, item_id)
    rows = (
        await db.execute(
            select(KnowledgeVersion)
            .where(KnowledgeVersion.item_id == item.id)
            .order_by(KnowledgeVersion.version.desc())
        )
    ).scalars().all()
    return {
        "versions": [
            {
                "id": str(v.id), "version": v.version, "title": v.title,
                # The list is a picker, not a viewer: full bodies of every version of a
                # long doc add up to hundreds of KB per request. Rollback reads the
                # snapshot server-side; `full` serves the diff/full-text viewer.
                "content": v.content if full else v.content[:200],
                "content_truncated": (not full) and len(v.content) > 200,
                "category": v.category, "tags": v.tags, "editor_email": v.editor_email,
                "change_note": v.change_note,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in rows
        ]
    }


@router.post("/{item_id}/rollback/{version_id}")
async def rollback(
    item_id: str, version_id: uuid.UUID, request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    # version_id typed as UUID → malformed values get a 422 instead of a bare 500;
    # the service keeps its str signature, so pass it back as a string.
    item = await _get_or_404(db, item_id)
    result = await ksvc.rollback_to_version(db, item, str(version_id), user)
    if not result:
        raise HTTPException(status_code=404, detail="版本不存在")
    ver = await db.get(KnowledgeVersion, version_id)  # identity-map hit, no extra query
    await write_audit(
        db, user, "knowledge.rollback", "knowledge_item", str(item.id),
        {
            "version_id": str(version_id),
            "restored_version": ver.version if ver else None,
            "new_version": item.version,
        },
        ip=_client_ip(request),
    )
    await db.commit()
    return _item_dict(item)


# ----------------------------------------------------------------- import
@router.get("/import/template", response_class=PlainTextResponse)
async def import_template(user: AdminUser = Depends(get_current_user)):
    # Columns mirror /knowledge/export so an exported file re-imports as-is.
    return (
        "title,content,category,tags,status,effective_from,expires_at\n"
        "退货政策,本店支持7天无理由退货,售后,退货;售后,published,,\n"
        "618大促,活动期间全场9折,大促,促销,published,2026-06-01,2026-06-20\n"
    )


@router.post("/import")
async def import_file(
    file: UploadFile,
    request: Request,
    dry_run: bool = Query(False, description="true=只解析/查重并返回报告，不落库"),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    name = (file.filename or "").lower()
    if not name.endswith((".csv", ".json")):
        raise HTTPException(
            status_code=400,
            detail="仅支持 .csv / .json 文件，请先另存为 CSV（UTF-8）或 JSON 再上传",
        )
    fmt = "json" if name.endswith(".json") else "csv"
    data = await file.read()
    if data[:4] == b"PK\x03\x04":  # xlsx/zip renamed to .csv would parse into garbage rows
        raise HTTPException(
            status_code=400, detail="检测到 Excel 文件，请在 Excel 中另存为 CSV（UTF-8）后重新上传"
        )
    if len(data) > _IMPORT_MAX_BYTES:
        raise HTTPException(
            status_code=413, detail="文件超过 20MB 上限，请拆分成多个文件分批导入"
        )
    if dry_run:
        # Nothing persisted → nothing to audit.
        return await import_knowledge(db, data, fmt, user, dry_run=True)
    result = await import_knowledge(db, data, fmt, user)
    # Batch-level audit: the filename/encoding/counters exist nowhere else once the
    # response is dismissed. import_knowledge committed already; this is its own tx.
    await write_audit(
        db, user, "knowledge.import", "knowledge_item", "",
        {
            "filename": file.filename or "",
            "encoding": result.get("encoding", ""),
            "total": result.get("total", 0),
            "created": result.get("created", 0),
            "failed": result.get("failed", 0),
            "skipped_duplicates": result.get("skipped_duplicates", 0),
        },
        ip=_client_ip(request),
    )
    await db.commit()
    return result


# ----------------------------------------------------------------- test retrieval
class TestIn(BaseModel):
    query: str


@router.post("/test-retrieval")
async def test_retrieval(body: TestIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    # Pass-through: the service returns {"score_kind": "fusion"|"rerank"|None,
    # "results": [...]} so the UI can label the two incomparable score scales.
    return await ksvc.test_retrieval(db, body.query)


# ----------------------------------------------------------------- embedding status
@router.get("/embedding/status")
async def embedding_status(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    total = (await db.execute(select(func.count(KnowledgeChunk.id)))).scalar_one()
    ready = (
        await db.execute(select(func.count(KnowledgeChunk.id)).where(KnowledgeChunk.status == "ready"))
    ).scalar_one()
    job = (
        await db.execute(select(EmbeddingRebuildJob).order_by(EmbeddingRebuildJob.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    # Items saved/imported but not yet chunked: they are invisible to total/ready
    # (chunk rows don't exist yet), so right after a big import the progress bar would
    # freeze at 100% — this counter lets the UI keep polling and show "N 条排队中".
    queued_items = (
        await db.execute(
            text(
                "SELECT count(*) FROM knowledge_items i "
                "WHERE i.status <> 'archived' AND i.content <> '' "
                "  AND NOT EXISTS (SELECT 1 FROM knowledge_chunks c WHERE c.item_id = i.id)"
            )
        )
    ).scalar_one()
    return {
        "total_chunks": total,
        "ready_chunks": ready,
        "queued_items": int(queued_items),
        "rebuild": None
        if not job
        else {
            "id": str(job.id), "status": job.status, "progress": job.progress,
            "processed": job.processed_chunks, "total": job.total_chunks,
            "from_model": job.from_model, "to_model": job.to_model, "error": job.error,
        },
    }


# ----------------------------------------------------------------- chunks / re-embed
@router.get("/{item_id}/chunks")
async def item_chunks(
    item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    """How an item was split + which chunks have a vector (diagnose 'why isn't this found')."""
    item = await _get_or_404(db, item_id)
    chunks = await ksvc.list_item_chunks(db, str(item.id))
    ready = sum(1 for c in chunks if c["status"] == "ready")
    return {"item_id": str(item.id), "chunk_count": len(chunks), "ready_count": ready, "chunks": chunks}


@router.post("/{item_id}/reembed")
async def reembed_one(
    item_id: str, request: Request,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator),
):
    """Re-chunk + re-embed a single item (e.g. after an embedding outage)."""
    item = await _get_or_404(db, item_id)
    await ksvc.enqueue_reembed(str(item.id))
    await write_audit(
        db, user, "knowledge.reembed", "knowledge_item", str(item.id), None,
        ip=_client_ip(request),
    )
    await db.commit()
    return {"ok": True, "queued": True}


@router.post("/reembed-pending")
async def reembed_pending(
    request: Request,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator),
):
    """Re-embed every item whose chunks are missing/incomplete — one click to heal coverage
    after an embedding outage or the long-document batch fix."""
    ids = await ksvc.items_needing_embedding(db)
    for iid in ids:
        await ksvc.enqueue_reembed(iid)
    await write_audit(
        db, user, "knowledge.reembed_pending", "knowledge_item", "",
        {"queued": len(ids)}, ip=_client_ip(request),
    )
    await db.commit()
    return {"ok": True, "queued": len(ids)}


# ----------------------------------------------------------------- review queue
@router.get("/review/list")
async def review_list(
    status: str = ReviewStatus.PENDING,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    base = select(KnowledgeReviewCandidate).where(KnowledgeReviewCandidate.status == status)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base.order_by(
                KnowledgeReviewCandidate.created_at.desc(), KnowledgeReviewCandidate.id.desc()
            ).limit(200)
        )
    ).scalars().all()
    return {
        "total": int(total),  # > len(candidates) ⇒ the UI should flag truncation
        "candidates": [
            {
                "id": str(c.id), "raw_excerpt": c.raw_excerpt, "suggested_title": c.suggested_title,
                "suggested_content": c.suggested_content, "suggested_category": c.suggested_category,
                "status": c.status,
                # Provenance so the reviewer can jump to the source conversation
                # (Conversations 页已支持 ?session=ID 深链).
                "source_session_id": str(c.source_session_id) if c.source_session_id else None,
                "source_message_id": str(c.source_message_id) if c.source_message_id else None,
                "reviewer_id": str(c.reviewer_id) if c.reviewer_id else None,
                "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
                "created_item_id": str(c.created_item_id) if c.created_item_id else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
    }


class ReviewApproveIn(BaseModel):
    title: str | None = Field(None, max_length=_TITLE_MAX)
    content: str | None = None
    category: str | None = Field(None, max_length=_CATEGORY_MAX)
    # Distilled knowledge is often time-boxed (promos, notices): let the reviewer set
    # tags/status/validity window in the same step instead of a follow-up edit.
    tags: list[str] | None = None
    status: StatusLiteral | None = None
    effective_from: datetime | None = None
    expires_at: datetime | None = None

    @field_validator("effective_from", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return _coerce_utc(v)

    @model_validator(mode="after")
    def _window_ordered(self):
        if self.effective_from and self.expires_at and self.expires_at <= self.effective_from:
            raise ValueError("expires_at 必须晚于 effective_from")
        return self


async def _pending_candidate_or_409(
    db: AsyncSession, cid: uuid.UUID
) -> KnowledgeReviewCandidate:
    """404 for unknown ids; 409 (who/when) for already-reviewed ones — a stale list is
    the everyday path here, and 「不存在」 would send the reviewer chasing ghosts."""
    # FOR UPDATE: a concurrent approve/reject of the same candidate waits for this transaction
    # and then sees it's no longer pending (409) — instead of both creating a knowledge item.
    cand = (
        await db.execute(
            select(KnowledgeReviewCandidate)
            .where(KnowledgeReviewCandidate.id == cid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if not cand:
        raise HTTPException(status_code=404, detail="待审核条目不存在")
    if cand.status != ReviewStatus.PENDING:
        reviewer = ""
        if cand.reviewer_id:
            admin = await db.get(AdminUser, cand.reviewer_id)
            reviewer = admin.email if admin else ""
        when = cand.reviewed_at.strftime("%Y-%m-%d %H:%M") if cand.reviewed_at else "此前"
        verdict = "通过" if cand.status == ReviewStatus.APPROVED else "拒绝"
        raise HTTPException(
            status_code=409,
            detail=f"该候选已被 {reviewer or '其他管理员'} 于 {when} {verdict}，请刷新审核列表",
        )
    return cand


@router.post("/review/{cid}/approve")
async def review_approve(
    cid: uuid.UUID, body: ReviewApproveIn, request: Request,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    cand = await _pending_candidate_or_409(db, cid)
    title = (body.title or cand.suggested_title).strip()
    content = body.content or cand.suggested_content
    # Distilled candidates are, by construction, paraphrases of answers that already
    # cited the KB — approving them verbatim accumulates near-duplicates that crowd
    # top_k. Block exact duplicates (same title, or same content modulo whitespace);
    # the candidate stays pending so the reviewer can edit-and-approve or reject.
    norm_content = re.sub(r"\s+", "", content)
    dup = (
        await db.execute(
            text(
                "SELECT id::text, title FROM knowledge_items "
                "WHERE status <> 'archived' "
                "  AND ((:title <> '' AND title = :title) "
                "       OR (:norm <> '' AND regexp_replace(content, '\\s+', '', 'g') = :norm)) "
                "LIMIT 1"
            ),
            {"title": title, "norm": norm_content},
        )
    ).first()
    if dup:
        # Structured detail: the frontend renders the message AND builds a
        # /knowledge?item=<id> jump link to the existing entry.
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"与已有条目重复：{dup[1] or dup[0]}，请修改标题/内容后再通过，或改为更新原条目",
                "item_id": dup[0],
                "title": dup[1],
            },
        )
    item = await ksvc.create_item(
        db,
        {
            "title": title,
            "content": content,
            "category": body.category or cand.suggested_category,
            "tags": body.tags or [],
            "status": body.status or KnowledgeStatus.PUBLISHED,
            "source": KnowledgeSource.AUTO_DISTILLED,
        },
        user,
    )
    # Validity window is set on the flushed row (create_item's dict API doesn't carry
    # it) — same pattern as the create endpoint.
    item.effective_from = body.effective_from
    item.expires_at = body.expires_at
    now = datetime.now(UTC)
    cand.status = ReviewStatus.APPROVED
    cand.reviewer_id = user.id
    cand.reviewed_at = now
    cand.created_item_id = item.id

    # Same FAQ liked in several sessions ⇒ several near-identical pending candidates
    # (distill only dedupes by source_message_id). Approving one resolves its twins:
    # auto-reject pending siblings with the same fingerprint (identical suggested
    # title, or identical whitespace-normalised suggested content).
    sib_title = cand.suggested_title.strip()
    sib_norm = re.sub(r"\s+", "", cand.suggested_content)
    sib_conds = []
    if sib_title:
        sib_conds.append(KnowledgeReviewCandidate.suggested_title == sib_title)
    if sib_norm:
        sib_conds.append(
            func.regexp_replace(KnowledgeReviewCandidate.suggested_content, r"\s+", "", "g")
            == sib_norm
        )
    auto_rejected = 0
    if sib_conds:
        res = await db.execute(
            update(KnowledgeReviewCandidate)
            .where(
                KnowledgeReviewCandidate.status == ReviewStatus.PENDING,
                KnowledgeReviewCandidate.id != cand.id,
                or_(*sib_conds),
            )
            .values(status=ReviewStatus.REJECTED, reviewer_id=user.id, reviewed_at=now)
            .execution_options(synchronize_session=False)
        )
        auto_rejected = res.rowcount or 0

    await write_audit(
        db, user, "knowledge.review_approve", "knowledge_review_candidate", str(cand.id),
        {"item_id": str(item.id), "auto_rejected_duplicates": auto_rejected},
        ip=_client_ip(request),
    )
    await db.commit()
    return {"ok": True, "item_id": str(item.id), "auto_rejected_duplicates": auto_rejected}


@router.post("/review/{cid}/reject")
async def review_reject(
    cid: uuid.UUID, request: Request,
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator),
):
    cand = await _pending_candidate_or_409(db, cid)
    cand.status = ReviewStatus.REJECTED
    cand.reviewer_id = user.id
    cand.reviewed_at = datetime.now(UTC)
    await write_audit(
        db, user, "knowledge.review_reject", "knowledge_review_candidate", str(cand.id),
        None, ip=_client_ip(request),
    )
    await db.commit()
    return {"ok": True}
