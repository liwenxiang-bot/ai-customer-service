"""Canned (quick-reply) responses: CRUD for operator templates used in the workbench."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_operator
from app.db.session import get_db
from app.models.admin import AdminUser
from app.models.conversation import CannedResponse

router = APIRouter(prefix="/canned", tags=["admin-canned"])


def _ser(c: CannedResponse) -> dict:
    return {
        "id": str(c.id),
        "title": c.title,
        "content": c.content,
        "category": c.category,
        "sort_order": c.sort_order,
    }


@router.get("")
async def list_canned(
    q: str = "", db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    stmt = select(CannedResponse)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(CannedResponse.title.ilike(like), CannedResponse.content.ilike(like)))
    stmt = stmt.order_by(CannedResponse.sort_order, CannedResponse.created_at.desc())
    rows = (await db.execute(stmt)).scalars().all()
    return {"items": [_ser(c) for c in rows]}


class CannedIn(BaseModel):
    title: str = ""
    content: str
    category: str = ""
    sort_order: int = 0


@router.post("")
async def create_canned(
    body: CannedIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    c = CannedResponse(
        title=body.title, content=body.content, category=body.category, sort_order=body.sort_order
    )
    db.add(c)
    await db.flush()
    out = _ser(c)
    await db.commit()
    return out


@router.put("/{cid}")
async def update_canned(
    cid: str, body: CannedIn, db: AsyncSession = Depends(get_db),
    user: AdminUser = Depends(require_operator),
):
    c = await db.get(CannedResponse, uuid.UUID(cid))
    if not c:
        raise HTTPException(status_code=404, detail="模板不存在")
    c.title, c.content, c.category, c.sort_order = (
        body.title, body.content, body.category, body.sort_order
    )
    await db.flush()
    out = _ser(c)
    await db.commit()
    return out


@router.delete("/{cid}")
async def delete_canned(
    cid: str, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_operator)
):
    c = await db.get(CannedResponse, uuid.UUID(cid))
    if c:
        await db.delete(c)
        await db.commit()
    return {"ok": True}
