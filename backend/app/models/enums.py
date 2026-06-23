"""String-valued enums used across models.

We store these as plain VARCHAR (not PG ENUM types) to keep migrations painless —
adding a value never requires an ALTER TYPE.
"""

from __future__ import annotations

from enum import StrEnum


class AdminRole(StrEnum):
    ADMIN = "admin"      # full control
    OPERATOR = "operator"  # day-to-day ops: knowledge, conversations, handoff
    READONLY = "readonly"  # view only


class ChannelType(StrEnum):
    WEB = "web"
    WECHAT_WORK = "wechat_work"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    CLOSED = "closed"
    ESCALATED = "escalated"      # handed off, awaiting human
    HUMAN_HANDLED = "human_handled"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class FeedbackKind(StrEnum):
    UP = "up"
    DOWN = "down"


class KnowledgeStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class KnowledgeSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    AUTO_DISTILLED = "auto_distilled"


class ChunkStatus(StrEnum):
    PENDING = "pending"      # awaiting embedding
    READY = "ready"
    STALE = "stale"          # embedding model changed; needs rebuild
    FAILED = "failed"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class HandoffStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class HandoffReason(StrEnum):
    USER_REQUEST = "user_request"
    MODEL_DECISION = "model_decision"
    NEGATIVE_FEEDBACK = "negative_feedback"
    ERROR_FALLBACK = "error_fallback"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
