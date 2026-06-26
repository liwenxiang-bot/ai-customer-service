"""Per-request tenant-context middleware — establishes the active tenant before any query.

Resolution order (first hit wins):
  1. Bearer JWT `tenant` claim — authenticated admin/operator requests
  2. chat `channel_key` → tenant — public widget/chat
  3. explicit slug (X-Tenant-Slug header or ?tenant=) — login, tenant-scoped tooling
  4. the implicit default tenant — single-tenant deploys / unspecified

Pure ASGI (not BaseHTTPMiddleware) so the contextvar set here actually propagates to the
route handler + its get_db. WebSocket scopes pass through untouched — those endpoints set the
tenant themselves (chat/ws.py, admin/ws.py), since the handshake carries the channel/token.
"""

from __future__ import annotations

import uuid

from starlette.requests import Request

from app.core.security import decode_token
from app.db.session import session_scope
from app.db.tenant_context import DEFAULT_TENANT_ID, tenant_scope
from app.services.tenant import tenant_for_channel, tenant_for_slug


async def _resolve(request: Request) -> uuid.UUID:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            claim = decode_token(auth[7:]).get("tenant")
            if claim:
                return uuid.UUID(str(claim))
        except Exception:  # noqa: BLE001 — fall through to other signals
            pass

    path = request.url.path
    if path.startswith("/api/chat") or path.startswith("/ws/chat"):
        key = request.query_params.get("channel_key")
        if key:
            async with session_scope() as db:
                tid = await tenant_for_channel(db, "web", key)
            if tid:
                return tid

    slug = request.headers.get("x-tenant-slug") or request.query_params.get("tenant")
    if slug:
        async with session_scope() as db:
            tid = await tenant_for_slug(db, slug)
        if tid:
            return tid

    return DEFAULT_TENANT_ID


class TenantContextMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        tid = await _resolve(Request(scope, receive))
        with tenant_scope(tid):
            await self.app(scope, receive, send)
