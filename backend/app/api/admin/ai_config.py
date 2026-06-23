"""AI configuration admin: LLM/embedding/rerank/retrieval + system prompt.

Secrets are write-only from the UI (sent to set, never echoed back — masked on read).
Changing the embedding model or dimension triggers a full vector rebuild.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.core.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.db.session import get_db
from app.llm.base import LLMSettings
from app.llm.factory import get_provider
from app.models.admin import AdminUser
from app.services.ai_config import get_active_ai_config
from app.services.audit import write_audit
from app.services.embedding_rebuild import latest_job, start_rebuild

router = APIRouter(prefix="/ai-config", tags=["admin-ai-config"])

_MASK = "••••••••"


def _serialize(cfg) -> dict:
    return {
        "id": str(cfg.id),
        "llm_provider": cfg.llm_provider,
        "llm_base_url": cfg.llm_base_url,
        "llm_api_key": mask_secret(cfg.llm_api_key_enc),
        "llm_model": cfg.llm_model,
        "llm_temperature": cfg.llm_temperature,
        "llm_max_tokens": cfg.llm_max_tokens,
        "system_prompt": cfg.system_prompt,
        "embedding_provider": cfg.embedding_provider,
        "embedding_base_url": cfg.embedding_base_url,
        "embedding_api_key": mask_secret(cfg.embedding_api_key_enc),
        "embedding_model": cfg.embedding_model,
        "embedding_dim": cfg.embedding_dim,
        "rerank_enabled": cfg.rerank_enabled,
        "rerank_base_url": cfg.rerank_base_url,
        "rerank_api_key": mask_secret(cfg.rerank_api_key_enc),
        "rerank_model": cfg.rerank_model,
        "retrieval": cfg.retrieval,
        "content_safety_enabled": cfg.content_safety_enabled,
        "semantic_cache_enabled": cfg.semantic_cache_enabled,
    }


@router.get("")
async def get_config(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    cfg = await get_active_ai_config(db)
    await db.commit()
    return _serialize(cfg)


class AIConfigUpdate(BaseModel):
    llm_provider: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    system_prompt: str | None = None
    embedding_provider: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    rerank_enabled: bool | None = None
    rerank_base_url: str | None = None
    rerank_api_key: str | None = None
    rerank_model: str | None = None
    retrieval: dict | None = None
    content_safety_enabled: bool | None = None
    semantic_cache_enabled: bool | None = None


def _maybe_secret(current: str | None, incoming: str | None) -> str | None:
    """Keep the existing secret if the field is blank or still the mask."""
    if incoming is None or incoming == "" or incoming == _MASK:
        return current
    return encrypt_secret(incoming)


@router.put("")
async def update_config(
    body: AIConfigUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)
):
    cfg = await get_active_ai_config(db)
    old_embed_model, old_embed_dim = cfg.embedding_model, cfg.embedding_dim

    data = body.model_dump(exclude_unset=True)

    # Secrets handled specially.
    if "llm_api_key" in data:
        cfg.llm_api_key_enc = _maybe_secret(cfg.llm_api_key_enc, data.pop("llm_api_key"))
    if "embedding_api_key" in data:
        cfg.embedding_api_key_enc = _maybe_secret(cfg.embedding_api_key_enc, data.pop("embedding_api_key"))
    if "rerank_api_key" in data:
        cfg.rerank_api_key_enc = _maybe_secret(cfg.rerank_api_key_enc, data.pop("rerank_api_key"))

    for field, value in data.items():
        setattr(cfg, field, value)
    await db.flush()

    # Embedding model/dim change → trigger full rebuild.
    rebuild = None
    if cfg.embedding_model != old_embed_model or cfg.embedding_dim != old_embed_dim:
        job = await start_rebuild(db, old_embed_model, old_embed_dim, cfg.embedding_model, cfg.embedding_dim)
        rebuild = {"job_id": str(job.id), "status": job.status}

    await write_audit(db, user, "ai_config.update", "ai_config", str(cfg.id),
                      {"embedding_changed": rebuild is not None})
    await db.commit()
    return {**_serialize(cfg), "rebuild": rebuild}


class TestLLMIn(BaseModel):
    message: str = "你好"


@router.post("/test-llm")
async def test_llm(body: TestLLMIn, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    """Round-trip a tiny prompt to verify the current LLM credentials/endpoint."""
    cfg = await get_active_ai_config(db)
    settings_ = LLMSettings(
        provider=cfg.llm_provider,
        base_url=cfg.llm_base_url,
        api_key=decrypt_secret(cfg.llm_api_key_enc) or "",
        model=cfg.llm_model,
        temperature=cfg.llm_temperature,
        max_tokens=64,
    )
    if not settings_.api_key:
        raise HTTPException(status_code=400, detail="未配置 LLM API Key")
    provider = get_provider(settings_)
    try:
        result = await provider.complete([{"role": "user", "content": body.message}], max_tokens=64)
        return {"ok": True, "reply": result.text, "model": cfg.llm_model}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"调用失败：{exc}")


@router.get("/rebuild-status")
async def rebuild_status(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    job = await latest_job(db)
    if not job:
        return {"rebuild": None}
    return {
        "rebuild": {
            "id": str(job.id), "status": job.status, "progress": job.progress,
            "processed": job.processed_chunks, "total": job.total_chunks, "error": job.error,
        }
    }
