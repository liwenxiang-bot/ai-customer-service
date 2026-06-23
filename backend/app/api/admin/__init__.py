"""Admin API router aggregator."""

from fastapi import APIRouter

from app.api.admin.accounts import router as accounts_router
from app.api.admin.ai_config import router as ai_config_router
from app.api.admin.auth import router as auth_router
from app.api.admin.channels import router as channels_router
from app.api.admin.conversations import router as conversations_router
from app.api.admin.dashboard import router as dashboard_router
from app.api.admin.handoff import router as handoff_router
from app.api.admin.knowledge import router as knowledge_router

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_router.include_router(auth_router)
admin_router.include_router(dashboard_router)
admin_router.include_router(knowledge_router)
admin_router.include_router(ai_config_router)
admin_router.include_router(channels_router)
admin_router.include_router(conversations_router)
admin_router.include_router(handoff_router)
admin_router.include_router(accounts_router)

__all__ = ["admin_router"]
