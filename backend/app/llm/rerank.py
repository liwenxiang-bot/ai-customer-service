"""Rerank client — Jina/Cohere/BGE-compatible `/rerank` endpoint.

Reranking is the second half of "answering accurately": after hybrid retrieval gathers
candidates, a cross-encoder reorders them by true relevance. Optional + configurable.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

log = get_logger("llm.rerank")


@dataclass(frozen=True)
class RerankSettings:
    base_url: str
    api_key: str
    model: str


class RerankClient:
    def __init__(self, cfg: RerankSettings) -> None:
        self.cfg = cfg
        # DashScope (通义 gte-rerank) uses a native body/response shape distinct from the
        # Jina/Cohere `/rerank` contract. Detect it and adapt transparently.
        self._is_dashscope = "dashscope" in cfg.base_url.lower()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _request(self, query: str, documents: list[str], top_n: int) -> tuple[str, dict]:
        n = min(top_n, len(documents))
        if self._is_dashscope:
            return self.cfg.base_url, {
                "model": self.cfg.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False, "top_n": n},
            }
        return self.cfg.base_url.rstrip("/") + "/rerank", {
            "model": self.cfg.model,
            "query": query,
            "documents": documents,
            "top_n": n,
        }

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """Return [(original_index, score), ...] sorted by score desc.

        On any failure, returns identity order so callers degrade gracefully to the
        pre-rerank ranking instead of erroring.
        """
        if not documents:
            return []
        try:
            url, body = self._request(query, documents, top_n)
            resp = await self._client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            # Jina/Cohere → {results}; DashScope → {output: {results}}.
            results = data.get("results") or data.get("output", {}).get("results", [])
            out = [
                (r["index"], float(r.get("relevance_score", r.get("score", 0.0))))
                for r in results
            ]
            out.sort(key=lambda x: x[1], reverse=True)
            return out
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("rerank_failed_degrading", error=str(exc))
            return [(i, 0.0) for i in range(min(top_n, len(documents)))]
