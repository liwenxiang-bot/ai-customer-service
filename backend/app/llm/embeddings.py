"""Embedding client (OpenAI-compatible /embeddings). Dimension is data-driven."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger

log = get_logger("llm.embeddings")

_QCACHE_MAX = 1024  # cap on cached query embeddings per client


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    api_key: str
    model: str
    dim: int
    # Providers cap inputs per /embeddings request (DashScope text-embedding-v4 = 10).
    # Exceeding it 400s the whole call; we split into batches of this size and concatenate.
    batch_size: int = 10


class EmbeddingClient:
    def __init__(self, cfg: EmbeddingSettings) -> None:
        self.cfg = cfg
        # LRU of query embeddings (deterministic for a fixed model). Lives with the client,
        # which the factory recreates on any config/model change — so it can't go stale.
        self._qcache: OrderedDict[str, list[float]] = OrderedDict()
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed any number of texts, transparently chunked under the provider's per-request
        cap. A long knowledge item (>10 chunks) used to 400 the whole call → stored
        unembedded → invisible to vector search; batching fixes that."""
        if not texts:
            return []
        bs = max(1, self.cfg.batch_size)
        if len(texts) <= bs:
            return await self._embed_batch(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), bs):
            out.extend(await self._embed_batch(texts[i : i + bs]))
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(
            "/embeddings", json={"model": self.cfg.model, "input": texts}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # Preserve request order (some providers return out of order).
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in ordered]

    async def embed_one(self, text: str) -> list[float]:
        """Embed a single query, cached. Queries (search + semantic-cache lookup) repeat far
        more than documents, so this trims a network round-trip + cost off the hot path."""
        key = (text or "").strip()
        if not key:
            return []
        cached = self._qcache.get(key)
        if cached is not None:
            self._qcache.move_to_end(key)
            return cached
        out = await self.embed([key])
        vec = out[0] if out else []
        if vec:
            self._qcache[key] = vec
            self._qcache.move_to_end(key)
            if len(self._qcache) > _QCACHE_MAX:
                self._qcache.popitem(last=False)
        return vec
