"""WebSocket chat endpoint — streaming, domain-whitelisted, rate-limited.

Wire protocol (server→client): connected, message_start, stream_chunk, tool_status,
citations, message_end, escalation, error, pong, history.
Client→server: user_message {text, client_msg_id}, feedback {message_id, kind},
history {limit}, ping.

Reconnect/backfill: the client passes its session_id and uid on the URL and may request
`history` to restore the transcript after a reconnect (requirements §5.1, §10).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.channels.base import InboundMessage
from app.channels.web import web_adapter
from app.core.logging import get_logger
from app.core.ratelimit import check_chat_limits
from app.db.session import session_scope
from app.models.conversation import Message as DBMessage
from app.models.enums import ChannelType, MessageRole
from app.services.channel import get_web_channel, is_origin_allowed
from app.services.conversation import handle_turn
from app.services.feedback import set_feedback

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

            if mtype != "user_message":
                continue

            text = (payload.get("text") or "").strip()
            if not text:
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

            new_sid = await _run_turn(
                websocket, end_user_id, ip, text, session_id, channel_key, payload
            )
            # Adopt the (possibly newly created) session id for the rest of the connection.
            session_id = new_sid or session_id

    except WebSocketDisconnect:
        log.info("ws_disconnect", end_user_id=end_user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_error", error=str(exc))
        try:
            await websocket.send_json({"type": "error", "message": "连接出现异常"})
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
    await ws.send_json(
        {
            "type": "history",
            "messages": [
                {
                    "id": str(m.id),
                    "role": m.role,
                    "content": m.content,
                    "citations": m.citations,
                    "feedback": m.feedback,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ],
        }
    )
