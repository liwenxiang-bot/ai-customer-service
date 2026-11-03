"""WebSocket chat endpoint — streaming, domain-whitelisted, rate-limited.

Wire protocol (server→client): connected, received {client_msg_id} (enqueue ACK),
message_start, stream_chunk, tool_status, citations, message_end (carries stopped=true
when the user aborted the turn), escalation, error, pong, history, human_message,
agent_typing (+ takeover status: human_takeover / ai_resumed / human_ended).
Client→server: user_message {text, client_msg_id}, feedback {message_id, kind},
history {limit}, request_human, end_session, typing, ping, stop. Unknown client message
types are silently ignored.

client_msg_id: the widget's per-message idempotency key. It is ACKed as soon as the
message is enqueued (AI and takeover turns alike), persisted with the user message
(session.meta, see conversation._record_client_msg_id), echoed on history user messages
so the client can match its optimistic bubbles, and a resend of an already-persisted id
never runs a second turn.

Reconnect/backfill: the client passes its session_id and uid on the URL and may request
`history` to restore the transcript after a reconnect — the latest N messages, delivered
in ascending seq order (requirements §5.1, §10).

Concurrency: a reader loop keeps draining frames while turns stream from a worker task,
so ping is answered during generation and {"type":"stop"} can cancel the in-flight turn
(the partial answer is persisted, then message_end {stopped: true} closes it).
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
from app.core.net import client_ip
from app.core.ratelimit import check_chat_limits
from app.core.redis_client import get_redis
from app.db.session import session_scope
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Message as DBMessage
from app.models.conversation import Session
from app.models.enums import ChannelType, HandoffReason, MessageRole, SessionStatus
from app.services.channel import get_web_channel, is_origin_allowed
from app.services.conversation import (
    _persist_assistant_text,
    _persist_user_message,
    _record_client_msg_id,
    get_or_create_session,
    handle_turn,
)
from app.services.feedback import set_feedback
from app.services.handoff import create_handoff
from app.services.takeover import (
    close_session,
    is_takeover,
    persist_customer_message,
    publish,
    push_channel,
)
from app.services.tenant import tenant_for_channel


async def _handle_request_human(session_id: str) -> None:
    """转人工请求：按会话当前状态判断，而非 escalated 标志——该标志从不复位，
    用它做守卫会让"接待过一次"的会话永远无法再转人工（客户却收到"已为你转接"
    的假回执）。接管中不重复建单（人工已在场）；其余状态交给 create_handoff：
    有存活工单则幂等复用，否则重新建单入队。"""
    if not session_id or not (sid := _as_uuid(session_id)):
        return
    async with session_scope() as db:
        s = await db.get(Session, sid)
        if s and s.status != SessionStatus.HUMAN_TAKEOVER:
            await create_handoff(
                db, s, HandoffReason.USER_REQUEST, "user_request",
                s.summary or s.title or "",
            )
        await db.commit()


async def _handle_end_session(session_id: str, payload: dict) -> None:
    """客户端结束会话：统一收尾（置 CLOSED + 解决存活工单 + 清接管标志）。"""
    if not session_id or not (sid := _as_uuid(session_id)):
        return
    rating = int(payload.get("rating") or 0)
    note = (payload.get("note") or "")[:1000]
    async with session_scope() as db:
        s = await db.get(Session, sid)
        if s:
            await close_session(db, s, rating=rating, note=note)
        await db.commit()


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None

router = APIRouter()
log = get_logger("chat.ws")

# Turns waiting behind the in-flight one, per connection. Small on purpose: the reader
# refuses (rate_limited frame) instead of buffering once it's full, so a client that
# stops reading responses can't grow process memory without bound (backpressure).
TURN_QUEUE_MAX = 4


def _client_msg_id(payload: dict) -> str:
    """The widget's idempotency key for a user_message frame (bounded, client-supplied)."""
    return str(payload.get("client_msg_id") or "")[:64]


def _sanitize_attachments(raw) -> list[dict]:
    """Coerce client-supplied attachments to list[dict], dropping anything malformed.

    The payload is untrusted: a non-array (string/number/object) or elements that aren't
    JSON objects would otherwise land in the DB verbatim and then crash the operator
    workbench that reads them back (it expects {url, name, ...} per attachment). Reject the
    whole field to [] when it isn't an array, and filter out non-object elements otherwise.
    """
    if not isinstance(raw, list):
        return []
    return [a for a in raw if isinstance(a, dict)]


async def _client_msg_seen(session_id: str, client_msg_id: str) -> bool:
    """True when this session already persisted a user message with this client_msg_id
    (client retry after a lost ACK) — the turn must not run again."""
    sid = _as_uuid(session_id)
    if sid is None:
        return False
    async with session_scope() as db:
        session = await db.get(Session, sid)
        pairs = (session.meta or {}).get("client_msg_ids", []) if session else []
    return any(p and p[0] == client_msg_id for p in pairs)


def _client_ip(ws: WebSocket) -> str:
    return client_ip(ws)


async def _reject_ws(websocket: WebSocket) -> None:
    """Policy rejection with an observable close code.

    Closing before accept() degrades to a plain HTTP 403, so the browser only
    sees a generic 1006 and the widget can't tell "blocked" from "network flake".
    Accept first, then close with 4403 so the client stops reconnecting.
    """
    await websocket.accept()
    await websocket.close(code=4403)


@router.websocket("/ws/chat")
async def chat_ws(
    websocket: WebSocket,
    uid: str = Query("", description="End-user id (anonymous UUID or real id)"),
    session_id: str = Query("", description="Existing session id to resume"),
    channel_key: str = Query("default"),
) -> None:
    origin = websocket.headers.get("origin", "")

    # Pin the tenant for this connection from the channel (resolver bypasses RLS) — the channel
    # row itself is tenant-scoped, so this must run before any other query. An unknown/disabled
    # channel (or suspended tenant) resolves to NULL → reject, never fall back to a tenant.
    async with session_scope() as db:
        tid = await tenant_for_channel(db, "web", channel_key)
    if tid is None:
        await _reject_ws(websocket)
        return
    set_current_tenant(tid)

    # ---- Domain whitelist enforced before any payload is processed ----
    async with session_scope() as db:
        channel = await get_web_channel(db, channel_key)
        if not channel.enabled:
            await _reject_ws(websocket)
            return
        if not is_origin_allowed(channel, origin):
            log.warning("ws_origin_rejected", origin=origin)
            await _reject_ws(websocket)
            return
        user_limit = channel.rate_limit_user_per_min
        ip_limit = channel.rate_limit_ip_per_min

    await websocket.accept()
    end_user_id = uid or f"anon-{uuid.uuid4().hex[:12]}"
    ip = _client_ip(websocket)

    # Single-writer guard: the main loop and the _push_listener task both emit on this socket,
    # and Starlette's send_json writes multiple frames and isn't concurrency-safe. Serialize
    # every send through one lock so a takeover push can't interleave with a streaming chunk
    # and corrupt/drop a frame. (admin/ws.py solves the same problem with a dedicated sender.)
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    await send({"type": "connected", "session_id": session_id or None, "end_user_id": end_user_id})

    # Background task that relays operator (takeover) messages to this socket.
    push_task: asyncio.Task | None = None
    push_sid = ""

    def ensure_push_listener(sid: str) -> None:
        # Follow the connection's *current* session: after "end session" the widget starts a
        # new one on the same socket, and operator pushes for it must still arrive. Also
        # restart a listener that exited (e.g. its Redis connection was lost for good).
        nonlocal push_task, push_sid
        if not sid:
            return
        if push_task is not None and push_sid == sid and not push_task.done():
            return
        if push_task is not None:
            push_task.cancel()
        push_sid = sid
        push_task = asyncio.create_task(_push_listener(send, sid))

    if session_id:
        ensure_push_listener(session_id)

    # ---- Reader/worker split ----
    # A single serial loop would starve ping while a turn streams (heartbeat watchdogs
    # kill the connection on long answers) and could never observe a "stop" frame. So
    # the reader below keeps draining frames while turns run in a worker task; turns
    # stay one-at-a-time per connection via the (bounded) queue, and `gen_task` is the
    # in-flight generation that a {"type":"stop"} frame cancels. `gen_state` is that
    # turn's shared state — its "done" flag tells stop handling the turn already
    # completed on the wire.
    turn_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=TURN_QUEUE_MAX)
    gen_task: asyncio.Task | None = None
    gen_state: dict | None = None

    async def process_turn(payload: dict) -> None:
        nonlocal session_id, gen_task, gen_state
        text = (payload.get("text") or "").strip()
        # Untrusted: reject malformed attachments (non-array / non-object items) so they
        # can't reach the DB and later crash the operator workbench that reads them back.
        attachments = _sanitize_attachments(payload.get("attachments"))
        client_msg_id = _client_msg_id(payload)

        # ---- Idempotency: a resend of an already-persisted message must not rerun ----
        # (client retry after a dropped ACK; the enqueue ACK already re-confirmed it).
        if client_msg_id and session_id and await _client_msg_seen(session_id, client_msg_id):
            return

        # ---- Rate limit (per user + per IP) ----
        decision = await check_chat_limits(end_user_id, ip, user_limit, ip_limit)
        if not decision.allowed:
            frame = {
                "type": "rate_limited",
                "retry_after": decision.retry_after,
                "message": "你发送得太快啦，请稍后再试。",
            }
            if client_msg_id:
                # so the widget can fail exactly this bubble, not guess from the tail
                frame["client_msg_id"] = client_msg_id
            await send(frame)
            return

        # ---- Human takeover: AI paused, just persist; the operator replies via push ----
        # (received was already ACKed at enqueue time, client_msg_id included.)
        if session_id and await is_takeover(session_id):
            async with session_scope() as db:
                msg = await persist_customer_message(db, session_id, text, attachments)
                if msg is not None and client_msg_id:
                    session = await db.get(Session, _as_uuid(session_id))
                    if session is not None:
                        _record_client_msg_id(session, client_msg_id, str(msg.id))
            return

        inbound = InboundMessage(
            channel_type=ChannelType.WEB,
            channel_key=channel_key,
            end_user_id=end_user_id,
            end_user_display=payload.get("display", ""),
            text=text,
            session_id=session_id or None,
            meta={"ip": ip, "ua": websocket.headers.get("user-agent", "")},
            attachments=attachments,
        )
        state: dict = {"partial": "", "turn_id": "", "done": False}
        task = asyncio.create_task(_run_turn(send, inbound, state, client_msg_id))
        gen_task = task
        gen_state = state
        try:
            # wait() (unlike awaiting the task) doesn't re-raise its cancellation here,
            # so a user stop is observed as task.cancelled() instead of killing us.
            await asyncio.wait({task})
        finally:
            gen_task = None
            gen_state = None
        if task.cancelled():
            if state.get("done"):
                # The normal message_end already went out before the cancel landed
                # (stop raced the turn's post-done commit, or the connection is being
                # torn down): the turn is closed on the wire — never re-persist it or
                # emit a second (stopped) message_end.
                session_id = state.get("session_id") or session_id
            else:
                # User-initiated stop: the cancel only rolled back the turn's AI
                # transaction (handle_turn commits the user message first, in its own
                # transaction), so persist whatever already streamed — the user message
                # is re-written only if the cancel beat that first commit — then close
                # the turn on the wire like a normal end (plus stopped=true).
                sid, msg_id = await _persist_stopped_turn(inbound, state["partial"], client_msg_id)
                session_id = sid or session_id
                await send(
                    {
                        "type": "message_end",
                        "stopped": True,
                        "turn_id": state["turn_id"],
                        "message_id": msg_id,
                        "session_id": session_id,
                        "citations": [],
                        "escalation": None,
                        "usage": None,
                        "degraded": False,
                    }
                )
        elif task.exception() is not None:
            log.warning("ws_turn_error", error=str(task.exception()))
            # The user message is likely already committed (handle_turn's first
            # transaction writes the session id back onto `inbound`) — adopt that
            # session so follow-up messages join it instead of orphaning it.
            session_id = inbound.session_id or session_id
            await send({"type": "error", "message": "连接出现异常"})
        else:
            # Adopt the (possibly newly created) session id for the rest of the connection.
            session_id = task.result() or session_id
        ensure_push_listener(session_id)

    async def turn_worker() -> None:
        while True:
            payload = await turn_queue.get()
            try:
                await process_turn(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep the connection alive
                log.warning("ws_turn_failed", error=str(exc))
                try:
                    await send({"type": "error", "message": "连接出现异常"})
                except Exception:  # noqa: BLE001 — socket gone
                    return

    worker_task = asyncio.create_task(turn_worker())

    try:
        while True:
            payload = await websocket.receive_json()
            mtype = payload.get("type")

            if mtype == "ping":
                await send({"type": "pong"})
                continue

            if mtype == "stop":
                # Cancel the in-flight generation; the worker persists the partial
                # answer and emits message_end {stopped: true}. No-op when idle, and
                # once the turn's message_end is on the wire ("done") a late stop must
                # not cancel the post-completion commit.
                if (
                    gen_task is not None
                    and not gen_task.done()
                    and not (gen_state or {}).get("done")
                ):
                    gen_task.cancel()
                continue

            if mtype == "typing":
                # Relay "customer is typing" to a watching operator (best-effort, fire-and-
                # forget). Only meaningful once a human is in the loop; the client throttles
                # and only sends while escalated, so this stays off the AI hot path.
                if session_id:
                    # session_id lets the operator workbench (watching several sessions)
                    # drop typing pushes that don't belong to the one on screen — same as
                    # human_message / customer_message.
                    await publish(
                        session_id,
                        {"type": "customer_typing", "session_id": session_id},
                    )
                continue

            if mtype == "history":
                await _send_history(send, session_id, int(payload.get("limit", 50)))
                continue

            if mtype == "feedback":
                async with session_scope() as db:
                    ok = await set_feedback(
                        db, payload.get("message_id", ""), payload.get("kind", ""),
                        payload.get("note", ""),
                    )
                await send({"type": "feedback_ack", "ok": ok})
                continue

            if mtype == "request_human":
                await _handle_request_human(session_id)
                await send(
                    {"type": "escalated", "message": "已为你转接人工，我们会尽快安排同事跟进。"}
                )
                continue

            if mtype == "end_session":
                await _handle_end_session(session_id, payload)
                await send({"type": "session_ended"})
                continue

            if mtype != "user_message":
                continue  # unknown message types are silently ignored (protocol contract)

            text = (payload.get("text") or "").strip()
            # Same sanitize as process_turn: a frame carrying only malformed attachments
            # (and no text) is empty once cleaned, so drop it instead of enqueuing it.
            attachments = _sanitize_attachments(payload.get("attachments"))
            if not text and not attachments:
                continue

            # Turns run serially in the worker so the reader stays free for ping/stop.
            # The queue is bounded: when the client outruns the worker we refuse the
            # frame right away instead of buffering it (memory backpressure).
            client_msg_id = _client_msg_id(payload)
            try:
                turn_queue.put_nowait(payload)
            except asyncio.QueueFull:
                frame = {
                    "type": "rate_limited",
                    "retry_after": 3,
                    "message": "你发送得太快啦，请稍后再试。",
                }
                if client_msg_id:
                    frame["client_msg_id"] = client_msg_id
                await send(frame)
                continue

            # Enqueue ACK: echoes client_msg_id so the widget can mark exactly this
            # bubble as delivered and clear its retry timer (AI and takeover turns).
            ack: dict = {"type": "received"}
            if client_msg_id:
                ack["client_msg_id"] = client_msg_id
            await send(ack)

    except WebSocketDisconnect:
        log.info("ws_disconnect", end_user_id=end_user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("ws_error", error=str(exc))
        try:
            await send({"type": "error", "message": "连接出现异常"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        # Cancel the worker before the generation task so its stop-handling branch
        # can't try to write to a socket that's already gone.
        for task in (worker_task, gen_task, push_task):
            if task is not None:
                task.cancel()


PUSH_RECONNECT_MAX_SEC = 10.0


async def _push_listener(send, session_id: str) -> None:
    """Subscribe to the session's Redis channel and forward operator pushes to the WS.

    `send` is the connection's locked writer (not the raw socket) so these pushes can't
    interleave with the main loop's frames.
    """
    channel = push_channel(session_id)
    backoff = 1.0
    while True:
        pubsub = get_redis().pubsub()
        try:
            await pubsub.subscribe(channel)
            backoff = 1.0
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    await send(json.loads(message["data"]))
                except Exception:  # noqa: BLE001 — socket gone: stop for good
                    return
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — Redis hiccup: resubscribe, don't go silent
            log.warning("push_listener_reconnect", session_id=session_id, error=str(exc))
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, PUSH_RECONNECT_MAX_SEC)


async def _run_turn(send, inbound: InboundMessage, state: dict, client_msg_id: str = "") -> str:
    """Stream one turn; return the resolved session id (created lazily on first turn).

    Runs as a cancellable task: `state` (accumulated partial text + turn id) outlives a
    cancellation so a user "stop" can persist what was already generated, and its "done"
    flag marks the point where message_end went out on the wire — a cancel landing after
    that (in the post-done commit window) must not re-persist or re-end the turn.
    Writes go through `send` (the connection's locked writer).
    """
    turn_id = uuid.uuid4().hex
    state["turn_id"] = turn_id
    await send({"type": "message_start", "turn_id": turn_id})

    resolved_sid = inbound.session_id or ""
    async with session_scope() as db:
        gen = handle_turn(db, inbound, client_msg_id)
        try:
            async for ev in gen:
                if ev.kind == "text":
                    state["partial"] += ev.text
                if ev.kind == "done" and ev.data.get("session_id"):
                    resolved_sid = ev.data["session_id"]
                rendered = web_adapter.render_event(ev)
                if rendered is not None:
                    rendered["turn_id"] = turn_id
                    await send(rendered)
                if ev.kind == "done":
                    # message_end is on the wire; the generator still has post-done work
                    # (cache store + COMMIT). Flag the turn complete so a stop that races
                    # this window is ignored instead of double-closing the turn.
                    state["session_id"] = resolved_sid
                    state["done"] = True
        finally:
            # Close the generator deterministically (esp. on cancel) while the DB
            # session is still open, so its finalization never outlives the scope.
            await gen.aclose()
    # Blocked/capped done events carry no session_id, but the session + user message
    # are already committed (transaction #1 wrote the id back onto `inbound`) — adopt
    # it so follow-up turns resume that session instead of orphaning it.
    return resolved_sid or inbound.session_id or ""


async def _user_message_persisted(db, session: Session, text: str, client_msg_id: str) -> bool:
    """Did handle_turn's first transaction already commit this turn's user message?

    Preferred check: the client_msg_id bookkeeping (session.meta) committed with it.
    Without an id (older clients), fall back to the session tail — that first
    transaction leaves the user message as the newest row, so a matching USER tail
    means it was committed before the cancel landed.
    """
    if client_msg_id:
        return any(
            p and p[0] == client_msg_id
            for p in (session.meta or {}).get("client_msg_ids", [])
        )
    row = (
        await db.execute(
            select(DBMessage)
            .where(DBMessage.session_id == session.id)
            .order_by(DBMessage.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None and row.role == MessageRole.USER and row.content == text


async def _persist_stopped_turn(
    inbound: InboundMessage, partial: str, client_msg_id: str = ""
) -> tuple[str, str | None]:
    """Persist a user-stopped turn in a fresh transaction; return (session_id, message_id).

    handle_turn commits the user message in its own first transaction (and writes the
    resolved session id back onto `inbound`), so a cancel normally only rolls back the
    AI half — here we just append whatever text already streamed. The user message is
    re-written ONLY when the cancel beat that first commit (session row missing, or no
    persisted message matches this client_msg_id / the session tail) — never twice.
    """
    async with session_scope() as db:
        session, created = await get_or_create_session(db, inbound)
        if created or not await _user_message_persisted(db, session, inbound.text, client_msg_id):
            await _persist_user_message(
                db, session, inbound.text, inbound.attachments, client_msg_id
            )
        msg_id: str | None = None
        if partial:
            msg = await _persist_assistant_text(db, session, partial, [])
            msg_id = str(msg.id)
        await db.commit()
        return str(session.id), msg_id


async def _send_history(send, session_id: str, limit: int) -> None:
    if not session_id:
        await send({"type": "history", "messages": []})
        return
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        await send({"type": "history", "messages": []})
        return
    async with session_scope() as db:
        # Latest N (seq desc + limit), then reversed in memory: the client always gets
        # the most recent window of a long session, still oldest→newest for rendering.
        rows = (
            await db.execute(
                select(DBMessage)
                .where(
                    DBMessage.session_id == sid,
                    DBMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
                )
                .order_by(DBMessage.seq.desc())
                .limit(limit)
            )
        ).scalars().all()
        session = await db.get(Session, sid)
    rows = list(reversed(rows))
    # client_msg_id lets the widget match a persisted user message to its optimistic
    # bubble on reconnect (replace, not duplicate). Only user messages ever have one.
    id_to_client = {
        p[1]: p[0]
        for p in ((session.meta or {}).get("client_msg_ids", []) if session else [])
        if isinstance(p, list) and len(p) == 2
    }
    await send(
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
                    "client_msg_id": id_to_client.get(str(m.id)),
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in rows
            ],
        }
    )
