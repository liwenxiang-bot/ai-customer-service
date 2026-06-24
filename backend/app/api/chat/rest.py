"""HTTP chat endpoints: public config bootstrap, non-streaming fallback, feedback, history."""

from __future__ import annotations

import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundMessage
from app.core import storage
from app.core.ratelimit import check_chat_limits
from app.db.session import get_db
from app.models.conversation import Message as DBMessage
from app.models.enums import ChannelType, MessageRole
from app.services.channel import get_web_channel, is_origin_allowed, public_branding
from app.services.conversation import handle_turn
from app.services.feedback import set_feedback

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Attachment upload constraints.
ALLOWED_UPLOAD_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf", "text/plain", "text/csv",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")


@router.get("/config")
async def chat_config(
    channel_key: str = Query("default"),
    origin: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    channel = await get_web_channel(db, channel_key)
    await db.commit()
    return {
        "allowed": is_origin_allowed(channel, origin or ""),
        "branding": public_branding(channel),
    }


@router.post("/upload")
async def chat_upload(
    request: Request,
    file: UploadFile = File(...),
    channel_key: str = Form("default"),
    uid: str = Form(""),
    origin: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Store a customer attachment (image/doc) and return a descriptor for the next message."""
    channel = await get_web_channel(db, channel_key)
    await db.commit()
    if not channel.enabled or not is_origin_allowed(channel, origin or ""):
        raise HTTPException(status_code=403, detail="origin not allowed")
    if not channel.settings.get("file_upload_enabled", True):
        raise HTTPException(status_code=403, detail="文件上传未开启")

    end_user_id = uid or f"anon-{uuid.uuid4().hex[:12]}"
    decision = await check_chat_limits(
        end_user_id, _ip(request), channel.rate_limit_user_per_min, channel.rate_limit_ip_per_min
    )
    if not decision.allowed:
        raise HTTPException(status_code=429, detail="上传过于频繁，请稍后再试")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(status_code=415, detail="不支持的文件类型")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大（上限 10MB）")

    url = await storage.put_object(data, content_type, prefix=f"chat/{channel_key}")
    return {
        "url": url,
        "name": file.filename or "file",
        "content_type": content_type,
        "size": len(data),
        "kind": "image" if content_type.startswith("image/") else "file",
    }


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    uid: str = ""
    session_id: str | None = None
    channel_key: str = "default"
    display: str = ""
    attachments: list[dict] = []


@router.post("/message")
async def chat_message(
    body: MessageIn,
    request: Request,
    origin: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Non-streaming fallback. Aggregates the streamed turn into one response."""
    channel = await get_web_channel(db, body.channel_key)
    if not channel.enabled or not is_origin_allowed(channel, origin or ""):
        raise HTTPException(status_code=403, detail="origin not allowed")

    end_user_id = body.uid or f"anon-{uuid.uuid4().hex[:12]}"
    decision = await check_chat_limits(
        end_user_id, _ip(request), channel.rate_limit_user_per_min, channel.rate_limit_ip_per_min
    )
    if not decision.allowed:
        raise HTTPException(status_code=429, detail=f"rate limited, retry after {decision.retry_after}s")

    inbound = InboundMessage(
        channel_type=ChannelType.WEB,
        channel_key=body.channel_key,
        end_user_id=end_user_id,
        end_user_display=body.display,
        text=body.text,
        session_id=body.session_id,
        meta={"ip": _ip(request), "ua": request.headers.get("user-agent", "")},
        attachments=body.attachments or [],
    )

    text_parts: list[str] = []
    final: dict = {}
    async for ev in handle_turn(db, inbound):
        if ev.kind == "text":
            text_parts.append(ev.text)
        elif ev.kind == "done":
            final = ev.data
    return {
        "reply": "".join(text_parts),
        "message_id": final.get("message_id"),
        "session_id": final.get("session_id"),
        "citations": final.get("citations", []),
        "escalation": final.get("escalation"),
    }


class FeedbackIn(BaseModel):
    message_id: str
    kind: str  # up | down
    note: str = ""


@router.post("/feedback")
async def chat_feedback(body: FeedbackIn, db: AsyncSession = Depends(get_db)):
    ok = await set_feedback(db, body.message_id, body.kind, body.note)
    await db.commit()
    if not ok:
        raise HTTPException(status_code=400, detail="invalid feedback")
    return {"ok": True}


@router.get("/history")
async def chat_history(
    session_id: str,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        return {"messages": []}
    rows = (
        await db.execute(
            select(DBMessage)
            .where(
                DBMessage.session_id == sid,
                DBMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
            )
            .order_by(DBMessage.seq.asc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "citations": m.citations,
                "attachments": m.attachments,
                "feedback": m.feedback,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ]
    }
