"""AI configuration admin: LLM/embedding/rerank/retrieval + system prompt.

Secrets are write-only from the UI (sent to set, never echoed back — masked on read).
Changing the embedding model or dimension triggers a full vector rebuild.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.config import settings
from app.core.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.core.net import UnsafeURL, check_outbound_url
from app.db.session import get_db
from app.llm.base import LLMSettings
from app.llm.factory import get_provider
from app.models.admin import AdminUser
from app.models.enums import KnowledgeStatus
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.services.abuse_guard import DEFAULT_OFF_TOPIC_REPLY, DEFAULT_SCOPE
from app.services.ai_config import get_active_ai_config, merged_retrieval
from app.services.audit import write_audit
from app.services.embedding_rebuild import latest_job, start_rebuild
from app.services.retrieval_eval import run_calibration

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
        "retrieval": merged_retrieval(cfg),
        "content_safety_enabled": cfg.content_safety_enabled,
        "semantic_cache_enabled": cfg.semantic_cache_enabled,
        "guard_enabled": cfg.guard_enabled,
        "business_scope": cfg.business_scope,
        "off_topic_reply": cfg.off_topic_reply,
        "max_input_chars": cfg.max_input_chars,
        "default_business_scope": DEFAULT_SCOPE,
        "default_off_topic_reply": DEFAULT_OFF_TOPIC_REPLY,
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
    guard_enabled: bool | None = None
    business_scope: str | None = Field(None, max_length=500)
    off_topic_reply: str | None = Field(None, max_length=500)
    max_input_chars: int | None = Field(None, ge=100, le=8000)
    # Required acknowledgement when the embedding model/dim changes (triggers a full,
    # disruptive vector rebuild) — without it the update is rejected with a 409 precheck.
    confirm_rebuild: bool = False


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
    confirm_rebuild = data.pop("confirm_rebuild", False)
    for field in ("llm_base_url", "embedding_base_url", "rerank_base_url"):
        if data.get(field):
            try:
                await asyncio.to_thread(check_outbound_url, data[field])
            except UnsafeURL as exc:
                raise HTTPException(status_code=400, detail=f"{field}：{exc}")

    # Precheck before mutating anything: an embedding model/dim change clears every
    # stored vector and triggers a full rebuild (vector search degrades meanwhile), so
    # it must be an explicit two-step — 409 carries the blast radius for the UI to show.
    new_embed_model = data.get("embedding_model", old_embed_model)
    new_embed_dim = data.get("embedding_dim", old_embed_dim)
    # The vector columns' dimension is fixed by the deployment (EMBEDDING_DIM drives the ORM
    # column type and pgvector asserts it client-side). Accepting another dim here commits
    # the config and then every rebuild/ingest fails — refuse with the correct procedure.
    if new_embed_dim != old_embed_dim and new_embed_dim != settings.embedding_dim:
        raise HTTPException(
            status_code=409,
            detail=(
                f"向量维度必须与部署配置 EMBEDDING_DIM 一致（当前 {settings.embedding_dim}）。"
                "更换为不同维度的模型：先修改服务器 .env 的 EMBEDDING_DIM 并重启 backend 与 worker，"
                "再回到这里切换模型与维度（会触发全量重建）。"
            ),
        )
    if (new_embed_model != old_embed_model or new_embed_dim != old_embed_dim) and not confirm_rebuild:
        items = (
            await db.execute(
                select(func.count(KnowledgeItem.id)).where(
                    KnowledgeItem.status != KnowledgeStatus.ARCHIVED
                )
            )
        ).scalar_one()
        chunks = (await db.execute(select(func.count(KnowledgeChunk.id)))).scalar_one()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "embedding_rebuild_confirm",
                "items": items,
                "chunks": chunks,
                "message": (
                    f"检测到 Embedding 模型/维度变更：将清空并重建 {items} 个条目 / "
                    f"{chunks} 个分块的向量，期间向量检索降级为关键词检索。"
                    "确认无误后请带 confirm_rebuild=true 重新提交。"
                ),
            },
        )

    # Secrets handled specially.
    if "llm_api_key" in data:
        cfg.llm_api_key_enc = _maybe_secret(cfg.llm_api_key_enc, data.pop("llm_api_key"))
    if "embedding_api_key" in data:
        cfg.embedding_api_key_enc = _maybe_secret(cfg.embedding_api_key_enc, data.pop("embedding_api_key"))
    if "rerank_api_key" in data:
        cfg.rerank_api_key_enc = _maybe_secret(cfg.rerank_api_key_enc, data.pop("rerank_api_key"))

    # Retrieval: merge into the existing dict (ignore nulls) so a blank form field never
    # wipes an existing key.
    incoming_retrieval = data.pop("retrieval", None)
    if incoming_retrieval:
        cfg.retrieval = {
            **(cfg.retrieval or {}),
            **{k: v for k, v in incoming_retrieval.items() if v is not None},
        }

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
        await asyncio.to_thread(check_outbound_url, cfg.llm_base_url)
    except UnsafeURL as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        result = await provider.complete([{"role": "user", "content": body.message}], max_tokens=64)
        return {"ok": True, "reply": result.text, "model": cfg.llm_model}
    except Exception as exc:  # noqa: BLE001
        # Short, typed message only — never echo the remote response body (an internal service
        # behind base_url would otherwise be readable through this endpoint).
        raise HTTPException(status_code=502, detail=f"调用失败：{type(exc).__name__}（请检查地址、Key 与模型名）")


@router.post("/calibrate")
async def calibrate(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)):
    """Sweep the relevance floor over the labelled eval cases and recommend a value
    (services.retrieval_eval). Read-only: applying is a normal config update."""
    cfg = await get_active_ai_config(db)
    report = await run_calibration(db, cfg)
    await db.rollback()  # retrieval sets per-transaction GUCs; nothing to persist
    return report


@router.post("/rebuild")
async def trigger_rebuild(
    db: AsyncSession = Depends(get_db), user: AdminUser = Depends(require_admin)
):
    """Manually (re)build all embeddings with the current model/dim — used to retry a
    failed rebuild or refresh vectors without changing the model."""
    cfg = await get_active_ai_config(db)
    job = await start_rebuild(db, cfg.embedding_model, cfg.embedding_dim, cfg.embedding_model, cfg.embedding_dim)
    await write_audit(db, user, "ai_config.rebuild", "ai_config", str(cfg.id), {"manual": True})
    await db.commit()
    return {"ok": True, "job_id": str(job.id), "status": job.status}


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
