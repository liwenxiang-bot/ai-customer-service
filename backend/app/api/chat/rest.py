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
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import InboundMessage
from app.core import storage
from app.core.net import client_ip
from app.core.ratelimit import check_chat_limits
from app.db.session import get_db
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import ChannelType, MessageRole
from app.services.channel import (
    is_origin_allowed,
    public_branding,
    resolve_active_web_channel,
)
from app.services.conversation import handle_turn
from app.services.feedback import set_feedback

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Attachment upload constraints.
ALLOWED_UPLOAD_TYPES = storage.ALLOWED_UPLOAD_TYPES
MAX_UPLOAD_BYTES = storage.MAX_UPLOAD_BYTES


def _ip(request: Request) -> str:
    return client_ip(request)


@router.get("/config")
async def chat_config(
    channel_key: str = Query("default"),
    origin: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    channel = await resolve_active_web_channel(db, channel_key)
    await db.commit()
    if channel is None:
        # unknown / disabled channel, or suspended tenant — widget refuses to load
        return {"allowed": False, "branding": {}}
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
    channel = await resolve_active_web_channel(db, channel_key)
    await db.commit()
    if channel is None or not is_origin_allowed(channel, origin or ""):
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

    key = await storage.put_object(data, content_type, prefix=f"chat/{channel_key}")
    return storage.attachment_descriptor(key, file.filename or "file", content_type, len(data))


@router.get("/media/{key:path}")
async def chat_media(key: str):
    """Public proxy that streams a stored attachment back from object storage, so image
    URLs are reachable by the browser (and the LLM) without exposing MinIO directly."""
    try:
        data, content_type = await storage.fetch_object(key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="附件不存在") from exc
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})


class MessageIn(BaseModel):
    # Attachment-only messages are valid (parity with the WS path), so text may be empty
    # as long as attachments aren't — the validator rejects both-empty with a 422.
    text: str = Field("", max_length=8000)
    uid: str = ""
    session_id: str | None = None
    channel_key: str = "default"
    display: str = ""
    attachments: list[dict] = []
    client_msg_id: str = Field("", max_length=64)

    @model_validator(mode="after")
    def _text_or_attachments(self) -> MessageIn:
        if not self.text.strip() and not self.attachments:
            raise ValueError("消息内容与附件不能同时为空")
        return self


async def _duplicate_turn_reply(
    db: AsyncSession, session_id: str | None, client_msg_id: str
) -> dict | None:
    """Idempotency for the HTTP fallback — WS parity with ws._client_msg_seen.

    When this session already persisted a user message under this client_msg_id (client
    retry after a lost response; handle_turn commits that bookkeeping in its first
    transaction), the turn must NOT run again. Replay the answer the first run produced —
    but only the answer that belongs to THIS turn: a turn is exactly `user(seq N) +
    assistant(seq N+1)`, so we accept the assistant message at seq == user_msg.seq + 1
    only (tool calls are inlined on that assistant row's tool_calls, not separate seqs).
    If no such row exists (the first run was stopped without output / died mid-answer),
    this turn simply has no answer — return an empty reply, still without rerunning.
    Anchoring to seq+1 avoids replaying a *different* later turn's assistant answer as if
    it were the reply to this one.
    """
    if not client_msg_id or not session_id:
        return None
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        return None
    session = await db.get(Session, sid)
    pairs = (session.meta or {}).get("client_msg_ids", []) if session else []
    msg_id = next((p[1] for p in pairs if p and p[0] == client_msg_id), None)
    if msg_id is None:
        return None
    try:
        user_msg = await db.get(DBMessage, uuid.UUID(msg_id))
    except (ValueError, TypeError):
        user_msg = None
    reply = None
    if user_msg is not None:
        reply = (
            await db.execute(
                select(DBMessage)
                .where(
                    DBMessage.session_id == sid,
                    DBMessage.role == MessageRole.ASSISTANT,
                    DBMessage.seq == user_msg.seq + 1,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    return {
        "reply": reply.content if reply else "",
        "message_id": str(reply.id) if reply else None,
        "session_id": str(sid),
        "citations": (reply.citations if reply else None) or [],
        "escalation": None,
    }


@router.post("/message")
async def chat_message(
    body: MessageIn,
    request: Request,
    origin: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """Non-streaming fallback. Aggregates the streamed turn into one response."""
    channel = await resolve_active_web_channel(db, body.channel_key)
    if channel is None or not is_origin_allowed(channel, origin or ""):
        raise HTTPException(status_code=403, detail="origin not allowed")

    # ---- Idempotency: a resend of an already-persisted client_msg_id never reruns ----
    # (checked before the rate limiter, mirroring the WS path, so a legitimate retry
    # can't be refused for the turn it already paid for).
    duplicate = await _duplicate_turn_reply(db, body.session_id, body.client_msg_id)
    if duplicate is not None:
        return duplicate

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
    async for ev in handle_turn(db, inbound, body.client_msg_id):
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
