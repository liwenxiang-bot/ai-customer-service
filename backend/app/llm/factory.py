"""Builds and caches LLM/embedding/rerank clients keyed by their (frozen) settings.

Caching keeps httpx connection pools warm and means a config change transparently
produces a fresh client on next use (old one lingers until GC / shutdown).
"""

from __future__ import annotations

from app.llm.base import LLMProvider, LLMSettings
from app.llm.embeddings import EmbeddingClient, EmbeddingSettings
from app.llm.openai_provider import OpenAICompatProvider
from app.llm.rerank import RerankClient, RerankSettings

_providers: dict[LLMSettings, LLMProvider] = {}
_embedders: dict[EmbeddingSettings, EmbeddingClient] = {}
_rerankers: dict[RerankSettings, RerankClient] = {}


def get_provider(cfg: LLMSettings) -> LLMProvider:
    inst = _providers.get(cfg)
    if inst is None:
        inst = OpenAICompatProvider(cfg)
        _providers[cfg] = inst
    return inst


def get_embedding_client(cfg: EmbeddingSettings) -> EmbeddingClient:
    inst = _embedders.get(cfg)
    if inst is None:
        inst = EmbeddingClient(cfg)
        _embedders[cfg] = inst
    return inst


def get_rerank_client(cfg: RerankSettings) -> RerankClient:
    inst = _rerankers.get(cfg)
    if inst is None:
        inst = RerankClient(cfg)
        _rerankers[cfg] = inst
    return inst


async def close_all() -> None:
    for p in list(_providers.values()):
        await p.aclose()  # type: ignore[attr-defined]
    for e in list(_embedders.values()):
        await e.aclose()
    for r in list(_rerankers.values()):
        await r.aclose()
    _providers.clear()
    _embedders.clear()
    _rerankers.clear()
