"""FastAPI application entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.admin import admin_router
from app.api.admin.ws import router as admin_ws_router
from app.api.chat import chat_router
from app.config import settings
from app.core.health import full_health
from app.core.logging import configure_logging, get_logger, set_trace_id
from app.core.metrics import http_latency, http_requests
from app.core.redis_client import close_redis
from app.core.storage import ensure_bucket
from app.llm.factory import close_all as close_llm
from app.tasks.queue import close_pool

configure_logging()
log = get_logger("app")

_WIDGET_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "widget", "dist")
_AGENT_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "agent", "dist")


def _widget_version() -> str:
    """Content hash of the built widget: /chat references widget.js?v=<hash>, so every release
    gets a new URL and a stale browser cache can't keep the old widget alive."""
    try:
        with open(os.path.join(_WIDGET_DIST, "widget.js"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "dev"


_WIDGET_VERSION = _widget_version()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.app_env)
    settings.enforce_secure_defaults()
    try:
        await ensure_bucket()
    except Exception as exc:  # noqa: BLE001
        log.warning("bucket_init_failed", error=str(exc))
    yield
    await close_redis()
    await close_llm()
    await close_pool()
    log.info("shutdown")


app = FastAPI(
    title="AI Customer Service",
    version="0.1.0",
    description="多渠道智能客服系统（起步版）",
    lifespan=lifespan,
    # The full API map is a recon gift on a public domain: only expose it outside production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# The widget is embedded on arbitrary merchant origins, so the public chat/embed
# API must be reachable cross-origin. Auth is Bearer-header based (no cookies), and
# the chat API is gated per-channel by an allowed-domains whitelist at the app layer,
# so an open CORS policy here is safe (credentials disabled).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Establish the tenant context for every HTTP request (RLS). Pure-ASGI so the contextvar
# propagates to handlers; WS endpoints set it themselves.
from app.api.tenant_mw import TenantContextMiddleware  # noqa: E402

app.add_middleware(TenantContextMiddleware)


@app.middleware("http")
async def observability_mw(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex
    set_trace_id(trace_id)
    start = time.monotonic()
    path = request.url.path
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        http_requests.labels(request.method, "error", "500").inc()
        raise
    finally:
        _route = request.scope.get("route")
        http_latency.labels(request.method, getattr(_route, "path", None) or "other").observe(
            time.monotonic() - start
        )
    route = request.scope.get("route")
    label = getattr(route, "path", None) or ("/agent" if path.startswith("/agent") else ("/embed" if path.startswith("/embed") else "other"))
    http_requests.labels(request.method, label, str(status)).inc()
    response.headers["x-trace-id"] = trace_id
    _security_headers(path, response)
    _cache_headers(path, response)
    return response


def _cache_headers(path: str, response) -> None:
    """Static front-ends must never get stuck on an old build.

    widget.js has a fixed URL (merchants paste it into their pages) and StaticFiles sends only
    ETag/Last-Modified: without Cache-Control, browsers apply heuristic caching (~10% of the
    file's age) and can keep serving a months-old build for days without asking — customers
    simply don't see new features. `no-cache` = always revalidate (a cheap 304 when unchanged).
    Vite's content-hashed assets never change, so they can be cached forever."""
    if "cache-control" in response.headers:
        return  # e.g. the media proxy sets its own
    if path.startswith("/agent/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith(("/embed/", "/agent")):
        response.headers["Cache-Control"] = "no-cache"


def _security_headers(path: str, response) -> None:
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # The agent console and API docs must never be framed (clickjacking). /chat stays
    # frameable on purpose: merchants may embed it with an <iframe>.
    if path.startswith(("/agent", "/docs", "/redoc")):
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


# ---- Routers ----
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(admin_ws_router)


# ---- Ops endpoints ----
@app.get("/health", tags=["ops"])
async def health():
    return await full_health()


@app.get("/health/live", tags=["ops"])
async def liveness():
    return {"status": "ok"}


@app.get("/metrics", tags=["ops"])
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---- Embeddable widget assets + standalone chat page ----
if os.path.isdir(_WIDGET_DIST):
    app.mount("/embed", StaticFiles(directory=_WIDGET_DIST), name="embed")

# ---- Agent console SPA（坐席站，hash 路由，深链 /agent/#/s/{id} 无需后端 fallback）----
if os.path.isdir(_AGENT_DIST):
    app.mount("/agent", StaticFiles(directory=_AGENT_DIST, html=True), name="agent")


@app.get("/chat", response_class=HTMLResponse, tags=["chat"])
async def standalone_chat(channel_key: str = "default"):
    """Standalone full-page chat (loads the same widget in fullscreen mode)."""
    # channel_key is reflected into an inline <script>: whitelist its shape (tenant slugs are
    # [a-z0-9-], column is String(64)) and JSON-encode it with `<` escaped so it can never
    # close the script element, even if the whitelist is widened later.
    if not _CHANNEL_KEY_RE.fullmatch(channel_key):
        raise HTTPException(status_code=400, detail="invalid channel_key")
    config = json.dumps({"channelKey": channel_key, "mode": "fullscreen"}).replace("<", "\\u003c")
    html = _STANDALONE_HTML.replace("__ACS_CONFIG__", config).replace("__WIDGET_VERSION__", _WIDGET_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


_CHANNEL_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


@app.get("/", response_class=PlainTextResponse, include_in_schema=False)
async def root():
    return "AI Customer Service backend. See /docs, /chat, /health."


_STANDALONE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
  <title>在线客服</title>
  <style>html,body{margin:0;height:100%;background:#f5f6fa}</style>
</head>
<body>
  <div id="acs-root"></div>
  <script>
    window.ACS_CONFIG = __ACS_CONFIG__;
  </script>
  <script src="/embed/widget.js?v=__WIDGET_VERSION__"></script>
</body>
</html>
"""
