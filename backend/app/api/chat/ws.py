"""WebSocket chat endpoint — streaming, domain-whitelisted, rate-limited.

Wire protocol (server→client): connected, message_start, stream_chunk, tool_status,
citations, message_end, escalation, error, pong, history.
Client→server: user_message {text, client_msg_id}, feedback {message_id, kind},
history {limit}, ping.

Reconnect/backfill: the client passes its session_id and uid on the URL and may request
`history` to restore the transcript after a reconnect (requirements §5.1, §10).
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.channels.base import InboundMessage
from app.channels.web import web_adapter
from app.core.logging import get_logger
from app.core.ratelimit import check_chat_limits
from app.core.redis_client import get_redis
from app.db.session import session_scope
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import ChannelType, HandoffReason, MessageRole, SessionStatus
from app.services.channel import get_web_channel, is_origin_allowed
from app.services.conversation import handle_turn
from app.services.feedback import set_feedback
from app.services.handoff import create_handoff
from app.services.takeover import (
    is_takeover,
    persist_customer_message,
    push_channel,
)


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None

router = APIRouter()
log = get_logger("chat.ws")


def _client_ip(ws: WebSocket) -> str:
    xff = ws.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return ws.client.host if ws.client else ""


@router.websocket("/ws/chat")
async def chat_ws(
    websocket: WebSocket,
    uid: str = Query("", description="End-user id (anonymous UUID or real id)"),
    session_id: str = Query("", description="Existing session id to resume"),
    channel_key: str = Query("default"),
) -> None:
    origin = websocket.headers.get("origin", "")

    # ---- Domain whitelist enforced before accepting the socket ----
    async with session_scope() as db:
        channel = await get_web_channel(db, channel_key)
        if not channel.enabled:
            await websocket.close(code=4403)
            return
        if not is_origin_allowed(channel, origin):
            log.warning("ws_origin_rejected", origin=origin)
            await websocket.close(code=4403)
            return
        user_limit = channel.rate_limit_user_per_min
        ip_limit = channel.rate_limit_ip_per_min

    await websocket.accept()
    end_user_id = uid or f"anon-{uuid.uuid4().hex[:12]}"
    ip = _client_ip(websocket)

    await websocket.send_json(
        {"type": "connected", "session_id": session_id or None, "end_user_id": end_user_id}
    )

    # Background task that relays operator (takeover) messages to this socket.
    push_task: asyncio.Task | None = None

    def ensure_push_listener(sid: str) -> None:
        nonlocal push_task
        if sid and push_task is None:
            push_task = asyncio.create_task(_push_listener(websocket, sid))

    if session_id:
        ensure_push_listener(session_id)

    try:
        while True:
            payload = await websocket.receive_json()
            mtype = payload.get("type")

            if mtype == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if mtype == "history":
                await _send_history(websocket, session_id, int(payload.get("limit", 50)))
                continue

            if mtype == "feedback":
                async with session_scope() as db:
                    ok = await set_feedback(
                        db, payload.get("message_id", ""), payload.get("kind", ""),
                        payload.get("note", ""),
                    )
                await websocket.send_json({"type": "feedback_ack", "ok": ok})
                continue

            if mtype == "request_human":
                if session_id and (sid := _as_uuid(session_id)):
                    async with session_scope() as db:
                        s = await db.get(Session, sid)
                        if s and not s.escalated:
                            await create_handoff(
                                db, s, HandoffReason.USER_REQUEST, "user_request",
                                s.summary or s.title or "",
                            )
                        await db.commit()
                await websocket.send_json(
                    {"type": "escalated", "message": "已为你转接人工，我们会尽快安排同事跟进。"}
                )
                continue

            if mtype == "end_session":
                if session_id and (sid := _as_uuid(session_id)):
                    rating = int(payload.get("rating") or 0)
                    note = (payload.get("note") or "")[:1000]
                    async with session_scope() as db:
                        s = await db.get(Session, sid)
                        if s:
                            s.status = SessionStatus.CLOSED
                            if 1 <= rating <= 5:
                                s.satisfaction_rating = rating
                            if note:
                                s.satisfaction_note = note
                        await db.commit()
                await websocket.send_json({"type": "session_ended"})
                continue

            if mtype != "user_message":
                continue

            text = (payload.get("text") or "").strip()
            attachments = payload.get("attachments") or []
            if not text and not attachments:
                continue

            # ---- Rate limit (per user + per IP) ----
            decision = await check_chat_limits(end_user_id, ip, user_limit, ip_limit)
            if not decision.allowed:
                await websocket.send_json(
                    {
                        "type": "rate_limited",
                        "retry_after": decision.retry_after,
                        "message": "你发送得太快啦，请稍后再试。",
                    }
                )
                continue

            # ---- Human takeover: AI paused, just persist; the operator replies via push ----
            if session_id and await is_takeover(session_id):
                async with session_scope() as db:
                    await persist_customer_message(db, session_id, text, attachments)
                await websocket.send_json({"type": "received"})
                continue

            new_sid = await _run_turn(
                websocket, end_user_id, ip, text, session_id, channel_key, payload
            )
            # Adopt the (possibly newly created) session id for the rest of the connection.
            session_id = new_sid or session_id
            ensure_push_listener(session_id)

    except WebSocketDisconnect:
        log.info("ws_disconnect", end_user_id=end_user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_error", error=str(exc))
        try:
            await websocket.send_json({"type": "error", "message": "连接出现异常"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        if push_task is not None:
            push_task.cancel()


async def _push_listener(websocket: WebSocket, session_id: str) -> None:
    """Subscribe to the session's Redis channel and forward operator pushes to the WS."""
    pubsub = get_redis().pubsub()
    channel = push_channel(session_id)
    try:
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                await websocket.send_json(json.loads(message["data"]))
            except Exception:  # noqa: BLE001 — socket gone
                break
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("push_listener_error", error=str(exc))
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            pass


async def _run_turn(ws, end_user_id, ip, text, session_id, channel_key, payload) -> str:
    """Stream one turn; return the resolved session id (created lazily on first turn)."""
    turn_id = uuid.uuid4().hex
    await ws.send_json({"type": "message_start", "turn_id": turn_id})

    inbound = InboundMessage(
        channel_type=ChannelType.WEB,
        channel_key=channel_key,
        end_user_id=end_user_id,
        end_user_display=payload.get("display", ""),
        text=text,
        session_id=session_id or None,
        meta={"ip": ip, "ua": ws.headers.get("user-agent", "")},
        attachments=payload.get("attachments") or [],
    )

    resolved_sid = session_id
    async with session_scope() as db:
        async for ev in handle_turn(db, inbound):
            if ev.kind == "done" and ev.data.get("session_id"):
                resolved_sid = ev.data["session_id"]
            rendered = web_adapter.render_event(ev)
            if rendered is not None:
                rendered["turn_id"] = turn_id
                await ws.send_json(rendered)
    return resolved_sid


async def _send_history(ws: WebSocket, session_id: str, limit: int) -> None:
    if not session_id:
        await ws.send_json({"type": "history", "messages": []})
        return
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        await ws.send_json({"type": "history", "messages": []})
        return
    async with session_scope() as db:
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
        session = await db.get(Session, sid)
    await ws.send_json(
        {
            "type": "history",
            "status": session.status if session else "",
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role,
                    "content": m.content,
                    "citations": m.citations,
                    "attachments": m.attachments,
                    "feedback": m.feedback,
                    "from_human": m.model == "human",
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ],
        }
    )
