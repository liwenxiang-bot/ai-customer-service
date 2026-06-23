"""Chat API routers (WebSocket + REST)."""

from fastapi import APIRouter

from app.api.chat.rest import router as rest_router
from app.api.chat.wechat import router as wechat_router
from app.api.chat.ws import router as ws_router

chat_router = APIRouter()
chat_router.include_router(ws_router)
chat_router.include_router(rest_router)
chat_router.include_router(wechat_router)

__all__ = ["chat_router"]
