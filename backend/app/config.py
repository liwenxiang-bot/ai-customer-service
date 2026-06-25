"""Application configuration.

All settings come from environment variables (12-factor). Secrets and first-boot
defaults live here; *runtime-editable* config (LLM provider/model, retrieval params,
prompts, channel branding) is stored in the database and managed from the admin UI.
Env values act as the fallback / bootstrap for that DB-backed config.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# The canonical .env lives at the repo root. Resolve it absolutely so settings load
# correctly regardless of working directory: Makefile dev targets and `python -m
# scripts.*` run from backend/, where a bare ".env" is not found and settings would
# silently fall back to code defaults — e.g. EMBEDDING_DIM reverting to 1536 and
# breaking embedding writes when the active model is 1024-dim.
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files win; the absolute root .env overrides a CWD-relative one.
        env_file=(".env", str(_ROOT_ENV)),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    app_env: str = "development"
    app_debug: bool = True
    app_secret_key: str = "change-me"
    app_base_url: str = "http://localhost:8000"
    # Admin frontend base URL — used in handoff notification deep-links.
    # If empty, falls back to app_base_url.
    admin_base_url: str = ""

    # ---- Postgres ----
    postgres_user: str = "acs"
    postgres_password: str = "acs_dev_pass"
    postgres_db: str = "acs"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url: str | None = None

    # ---- Redis ----
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_url: str | None = None

    # ---- Object storage ----
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    minio_bucket: str = "acs-media"
    s3_region: str = "us-east-1"

    # ---- Auth ----
    jwt_secret: str = "change-me-jwt-secret"
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 14
    jwt_algorithm: str = "HS256"
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "admin12345"

    # ---- LLM defaults (fallback for DB-backed ai_configs) ----
    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024

    # ---- Embedding ----
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # ---- Rerank ----
    rerank_enabled: bool = False
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "bge-reranker-v2-m3"

    # ---- Retrieval ----
    rag_chunk_size: int = 600
    rag_chunk_overlap: int = 100
    rag_top_k: int = 5
    rag_vector_weight: float = 0.6
    rag_keyword_weight: float = 0.4
    # Semantic floor for the vector path: chunks whose cosine similarity to the query
    # is below this are dropped before fusion — this stops "always hit on the
    # nearest-but-irrelevant chunk". Calibrate per embedding model
    # (OpenAI ~0.2-0.35, BGE/Jina ~0.4-0.5).
    rag_vector_min_sim: float = 0.25
    # Final relevance floor applied to rerank scores when rerank is enabled (0 = off).
    rag_min_score: float = 0.0
    # Trigram similarity floor for the keyword path (exact-substring matches).
    rag_trgm_threshold: float = 0.1
    # Candidate pool before fusion/rerank = top_k * this (bigger → better rerank recall).
    rag_candidate_multiplier: int = 8

    # ---- Anti-abuse / cost ----
    rate_limit_user_per_min: int = 20
    rate_limit_ip_per_min: int = 60
    daily_cost_cap_usd: float = 10.0
    session_idle_timeout_minutes: int = 30
    max_tool_calls_per_turn: int = 6

    # ---- Optional features ----
    content_safety_enabled: bool = False
    semantic_cache_enabled: bool = False
    semantic_cache_threshold: float = 0.95

    # ---- Observability ----
    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # ---- Privacy ----
    data_retention_days: int = 180

    # ---- CORS ----
    admin_cors_origins: str = "http://localhost:5173"

    # ---- Single-tenant: the implicit default tenant id (tenant-ready) ----
    default_tenant_id: str = "00000000-0000-0000-0000-000000000001"

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_dsn(self) -> str:
        if self.redis_url:
            return self.redis_url
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.admin_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
