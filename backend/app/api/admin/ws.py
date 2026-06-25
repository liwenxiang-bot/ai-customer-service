"""Admin realtime WebSocket — pushes queue + watched-session events to the workbench.

Reuses the existing Redis pub/sub bus: a global `admin:events` channel for queue changes,
plus the per-session `chat:push:{id}` channel the client subscribes to via {type:"watch"}.

Auth: a short-lived access JWT passed as ?token= (the browser WebSocket API can't set an
Authorization header). Path is /ws/admin so it reuses the proxies' existing /ws/ upgrade
rule — no reverse-proxy changes needed.

Concurrency model (safe with redis-py async): each pub/sub object is owned by exactly one
relay task that only *listens*; messages funnel through a single asyncio.Queue to one
sender, so websocket.send_json is never called from two tasks at once.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.logging import get_logger
from app.core.redis_client import get_redis
from app.core.security import decode_token
from app.db.session import session_scope
from app.models.admin import AdminUser
from app.services.takeover import ADMIN_CHANNEL, push_channel

router = APIRouter()
log = get_logger("admin.ws")


async def _authed_user(token: str) -> AdminUser | None:
    if not token:
        return None
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            return None
        uid = payload["sub"]
    except (jwt.PyJWTError, KeyError):
        return None
    async with session_scope() as db:
        user = await db.get(AdminUser, uuid.UUID(uid))
        return user if user and user.is_active else None


@router.websocket("/ws/admin")
async def admin_ws(websocket: WebSocket, token: str = Query("")) -> None:
    user = await _authed_user(token)
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put({"type": "connected"})

    async def relay(channel: str) -> None:
        """Own one pub/sub, only listen, push messages onto the shared queue."""
        ps = get_redis().pubsub()
        try:
            await ps.subscribe(channel)
            async for m in ps.listen():
                if m.get("type") == "message":
                    try:
                        await queue.put(json.loads(m["data"]))
                    except Exception:  # noqa: BLE001 — skip malformed payloads
                        pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("admin_relay_error", error=str(exc))
        finally:
            try:
                await ps.unsubscribe(channel)
                await ps.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def sender() -> None:
        try:
            while True:
                await websocket.send_json(await queue.get())
        except Exception:  # noqa: BLE001 — socket gone
            pass

    sender_task = asyncio.create_task(sender())
    admin_task = asyncio.create_task(relay(ADMIN_CHANNEL))
    session_task: asyncio.Task | None = None

    try:
        while True:
            payload = await websocket.receive_json()
            t = payload.get("type")
            if t == "ping":
                await queue.put({"type": "pong"})
            elif t == "watch":
                sid = (payload.get("session_id") or "").strip()
                if session_task:
                    session_task.cancel()
                    session_task = None
                if sid:
                    session_task = asyncio.create_task(relay(push_channel(sid)))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("admin_ws_error", error=str(exc))
    finally:
        for tk in (sender_task, admin_task, session_task):
            if tk:
                tk.cancel()
