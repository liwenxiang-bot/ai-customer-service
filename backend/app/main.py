"""FastAPI application entrypoint."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", env=settings.app_env)
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
        http_requests.labels(request.method, path, "500").inc()
        raise
    finally:
        http_latency.labels(request.method, path).observe(time.monotonic() - start)
    http_requests.labels(request.method, path, str(status)).inc()
    response.headers["x-trace-id"] = trace_id
    return response


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


@app.get("/chat", response_class=HTMLResponse, tags=["chat"])
async def standalone_chat(channel_key: str = "default"):
    """Standalone full-page chat (loads the same widget in fullscreen mode)."""
    return HTMLResponse(_STANDALONE_HTML.replace("__CHANNEL_KEY__", channel_key))


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
    window.ACS_CONFIG = { channelKey: "__CHANNEL_KEY__", mode: "fullscreen" };
  </script>
  <script src="/embed/widget.js"></script>
</body>
</html>
"""
