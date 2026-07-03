"""Knowledge base admin: CRUD, versions/rollback, import, review queue, test retrieval."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.enums import KnowledgeSource, ReviewStatus
from app.models.knowledge import (
    EmbeddingRebuildJob,
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeReviewCandidate,
    KnowledgeVersion,
)
from app.services import knowledge as ksvc
from app.services.import_knowledge import import_knowledge

router = APIRouter(prefix="/knowledge", tags=["admin-knowledge"])


# ----------------------------------------------------------------- schemas
def _coerce_utc(v: datetime | None) -> datetime | None:
    """Naive input is taken as UTC so window comparisons and timestamptz storage are
    unambiguous regardless of the caller's serializer."""
    return v if v is None or v.tzinfo else v.replace(tzinfo=UTC)


class ItemIn(BaseModel):
    title: str = ""
    content: str
    category: str = ""
    tags: list[str] = []
    status: str = "published"
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
    title: str | None = None
    content: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    status: str | None = None
    # None clears the bound when the field is explicitly sent (checked via fields_set).
    effective_from: datetime | None = None
    expires_at: datetime | None = None
    change_note: str = "edited"

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
        "status": it.status,
        "source": it.source,
        "version": it.version,
        "chunk_count": chunk_count,
        "ready_count": ready_count,  # embedded chunks; < chunk_count ⇒ vectors missing
        "effective_from": it.effective_from.isoformat() if it.effective_from else None,
        "expires_at": it.expires_at.isoformat() if it.expires_at else None,
        "created_at": it.created_at.isoformat() if it.created_at else None,
        "updated_at": it.updated_at.isoformat() if it.updated_at else None,
    }


# ----------------------------------------------------------------- list / CRUD
@router.get("")
async def list_items(
    q: str = "",
    category: str = "",
    status: str = "",
    source: str = "",
    tag: str = "",
    expired: bool | None = Query(None, description="true=仅已过期条目，false=排除已过期"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    stmt = select(KnowledgeItem)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(KnowledgeItem.title.ilike(like), KnowledgeItem.content.ilike(like)))
    if category:
        stmt = stmt.where(KnowledgeItem.category == category)
    if status:
        stmt = stmt.where(KnowledgeItem.status == status)
    if source:
        stmt = stmt.where(KnowledgeItem.source == source)
    if tag:
        stmt = stmt.where(KnowledgeItem.tags.contains([tag]))  # JSONB @> [tag]
    if expired is not None:
        now = datetime.now(UTC)
        if expired:
            stmt = stmt.where(KnowledgeItem.expires_at.is_not(None), KnowledgeItem.expires_at <= now)
        else:
            stmt = stmt.where(
                or_(KnowledgeItem.expires_at.is_(None), KnowledgeItem.expires_at > now)
            )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(KnowledgeItem.updated_at.desc())
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
    # For the window fields None is a meaningful value ("clear the bound"), so apply
    # them only when explicitly sent, and validate against the merged final state.
    eff = body.effective_from if "effective_from" in body.model_fields_set else item.effective_from
    exp = body.expires_at if "expires_at" in body.model_fields_set else item.expires_at
    if eff and exp and exp <= eff:
        raise HTTPException(status_code=400, detail="expires_at 必须晚于 effective_from")
    data = {
        k: v
        for k, v in body.model_dump(exclude={"effective_from", "expires_at"}).items()
        if v is not None or k == "change_note"
    }
    await ksvc.update_item(db, item, data, user)
    item.effective_from = eff
    item.expires_at = exp
    await db.commit()
    return _item_dict(item)


@router.delete("/{item_id}")
async def delete_item(
    item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    item = await _get_or_404(db, item_id)
    await ksvc.delete_item(db, item, user)
    await db.commit()
    return {"ok": True}


# ----------------------------------------------------------------- versions
@router.get("/{item_id}/versions")
async def list_versions(item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
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
                "id": str(v.id), "version": v.version, "title": v.title, "content": v.content,
                "category": v.category, "tags": v.tags, "editor_email": v.editor_email,
                "change_note": v.change_note,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in rows
        ]
    }


@router.post("/{item_id}/rollback/{version_id}")
async def rollback(
    item_id: str, version_id: str, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    item = await _get_or_404(db, item_id)
    result = await ksvc.rollback_to_version(db, item, version_id, user)
    if not result:
        raise HTTPException(status_code=404, detail="版本不存在")
    await db.commit()
    return _item_dict(item)


# ----------------------------------------------------------------- import
@router.get("/import/template", response_class=PlainTextResponse)
async def import_template(user: AdminUser = Depends(get_current_user)):
    return "title,content,category,tags\n退货政策,本店支持7天无理由退货,售后,退货;售后\n"


@router.post("/import")
async def import_file(
    file: UploadFile, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    name = (file.filename or "").lower()
    fmt = "json" if name.endswith(".json") else "csv"
    data = await file.read()
    result = await import_knowledge(db, data, fmt, user)
    return result


# ----------------------------------------------------------------- test retrieval
class TestIn(BaseModel):
    query: str


@router.post("/test-retrieval")
async def test_retrieval(body: TestIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    return {"results": await ksvc.test_retrieval(db, body.query)}


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
    return {
        "total_chunks": total,
        "ready_chunks": ready,
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
    item_id: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    """Re-chunk + re-embed a single item (e.g. after an embedding outage)."""
    item = await _get_or_404(db, item_id)
    await ksvc.enqueue_reembed(str(item.id))
    return {"ok": True, "queued": True}


@router.post("/reembed-pending")
async def reembed_pending(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    """Re-embed every item whose chunks are missing/incomplete — one click to heal coverage
    after an embedding outage or the long-document batch fix."""
    ids = await ksvc.items_needing_embedding(db)
    for iid in ids:
        await ksvc.enqueue_reembed(iid)
    return {"ok": True, "queued": len(ids)}


# ----------------------------------------------------------------- review queue
@router.get("/review/list")
async def review_list(
    status: str = ReviewStatus.PENDING,
    db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(KnowledgeReviewCandidate)
            .where(KnowledgeReviewCandidate.status == status)
            .order_by(KnowledgeReviewCandidate.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return {
        "candidates": [
            {
                "id": str(c.id), "raw_excerpt": c.raw_excerpt, "suggested_title": c.suggested_title,
                "suggested_content": c.suggested_content, "suggested_category": c.suggested_category,
                "status": c.status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]
    }


class ReviewApproveIn(BaseModel):
    title: str | None = None
    content: str | None = None
    category: str | None = None


@router.post("/review/{cid}/approve")
async def review_approve(
    cid: str, body: ReviewApproveIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    cand = await db.get(KnowledgeReviewCandidate, uuid.UUID(cid))
    if not cand or cand.status != ReviewStatus.PENDING:
        raise HTTPException(status_code=404, detail="待审核条目不存在")
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
        raise HTTPException(
            status_code=409,
            detail=f"与已有条目重复：{dup[1] or dup[0]}，请修改标题/内容后再通过，或改为更新原条目",
        )
    item = await ksvc.create_item(
        db,
        {
            "title": title,
            "content": content,
            "category": body.category or cand.suggested_category,
            "source": KnowledgeSource.AUTO_DISTILLED,
        },
        user,
    )
    cand.status = ReviewStatus.APPROVED
    cand.reviewer_id = user.id
    cand.reviewed_at = datetime.now(UTC)
    cand.created_item_id = item.id
    await db.commit()
    return {"ok": True, "item_id": str(item.id)}


@router.post("/review/{cid}/reject")
async def review_reject(
    cid: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    cand = await db.get(KnowledgeReviewCandidate, uuid.UUID(cid))
    if not cand:
        raise HTTPException(status_code=404, detail="待审核条目不存在")
    cand.status = ReviewStatus.REJECTED
    cand.reviewer_id = user.id
    cand.reviewed_at = datetime.now(UTC)
    await db.commit()
    return {"ok": True}
