"""Import all models so `Base.metadata` is fully populated (Alembic, create_all)."""

from app.models.admin import AdminUser, AuditLog, RefreshToken
from app.models.config import AIConfig, ChannelConfig
from app.models.conversation import CannedResponse, HandoffTicket, Message, Session
from app.models.knowledge import (
    EmbeddingRebuildJob,
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeReviewCandidate,
    KnowledgeVersion,
)
from app.models.tenant import Tenant
from app.models.usage import SemanticCacheEntry, UsageDaily

__all__ = [
    "AdminUser",
    "RefreshToken",
    "AuditLog",
    "ChannelConfig",
    "AIConfig",
    "Session",
    "Message",
    "HandoffTicket",
    "CannedResponse",
    "KnowledgeItem",
    "KnowledgeChunk",
    "KnowledgeVersion",
    "KnowledgeReviewCandidate",
    "EmbeddingRebuildJob",
    "Tenant",
    "UsageDaily",
    "SemanticCacheEntry",
]
