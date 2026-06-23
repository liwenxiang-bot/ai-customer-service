"""Embedding client (OpenAI-compatible /embeddings). Dimension is data-driven."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.logging import get_logger

log = get_logger("llm.embeddings")


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    api_key: str
    model: str
    dim: int


class EmbeddingClient:
    def __init__(self, cfg: EmbeddingSettings) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=6), reraise=True)
    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.post(
            "/embeddings", json={"model": self.cfg.model, "input": texts}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # Preserve request order (some providers return out of order).
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in ordered]

    async def embed_one(self, text: str) -> list[float]:
        out = await self.embed([text])
        return out[0] if out else []
