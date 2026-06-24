"""Runtime-editable configuration: channels and AI settings.

These rows are the source of truth at runtime (env only seeds first boot). The admin
UI edits them; the AgentRunner / channel layer read the active row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class ChannelConfig(Base, TimestampMixin, TenantMixin):
    """One row per channel instance (web, wechat_work, ...).

    `settings` holds channel-specific config as JSON; secrets within it are
    encrypted at the service layer before persistence.
    """

    __tablename__ = "channel_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel_type", "key", name="uq_channel_tenant_type_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    channel_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Web branding / behaviour, WeChat creds, etc. Shape depends on channel_type.
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Web-channel anti-abuse (also stored here for per-channel override)
    allowed_domains: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit_user_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate_limit_ip_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional per-channel system prompt / persona override
    system_prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)

    @staticmethod
    def default_web_settings() -> dict:
        return {
            "welcome_message": "你好！我是智能客服助手，有什么可以帮你的吗？",
            "theme_color": "#4f46e5",
            "logo_url": "",
            "brand_name": "智能客服",
            "placeholder": "输入你的问题…",
            "default_theme": "light",  # light | dark
            "show_powered_by": True,
            "image_understanding_enabled": False,
            "file_upload_enabled": True,
            "suggested_questions": [],
        }


class AIConfig(Base, TimestampMixin, TenantMixin):
    """Active AI configuration: LLM provider/model/params, embedding, rerank, retrieval.

    Only one row per tenant is `is_active`. Switching embedding model/dim from here
    triggers a vector rebuild job (see services/ai_config).
    """

    __tablename__ = "ai_configs"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    # ---- LLM (OpenAI-compatible) ----
    llm_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="openai")
    llm_base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    llm_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted
    llm_model: Mapped[str] = mapped_column(String(120), nullable=False, default="gpt-4o-mini")
    llm_temperature: Mapped[float] = mapped_column(default=0.3, nullable=False)
    llm_max_tokens: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)

    # ---- System prompt / persona (global default; per-channel override on ChannelConfig) ----
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ---- Embedding ----
    embedding_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="openai")
    embedding_base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    embedding_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False, default="text-embedding-3-small")
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False, default=1536)

    # ---- Rerank ----
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rerank_base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    rerank_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    rerank_model: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    # ---- Retrieval params ----
    retrieval: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {
            "chunk_size": 600,
            "chunk_overlap": 100,
            "top_k": 5,
            "vector_weight": 0.6,
            "keyword_weight": 0.4,
            "min_score": 0.0,
            "rerank_top_n": 3,
        },
    )

    # ---- Optional features ----
    content_safety_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
