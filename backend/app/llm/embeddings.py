"""Embedding client (OpenAI-compatible /embeddings). Dimension is data-driven."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger

log = get_logger("llm.embeddings")

_QCACHE_MAX = 1024  # cap on cached query embeddings per client

# DashScope's OpenAI-compatible mode accepts `text_type` ('query'|'document') for
# asymmetric retrieval (short query → long document). Other providers may 400 on
# unknown fields, so passthrough is gated on looking like DashScope.
_DASHSCOPE_MODEL = re.compile(r"^text-embedding-v\d+$")
# Models that accept `dimensions` (matryoshka truncation): OpenAI text-embedding-3-*
# and DashScope text-embedding-v3/v4+. Older ones (ada-002, v1/v2) reject it.
_DIMENSIONS_MODEL = re.compile(r"^text-embedding-(3-|v[3-9])")


class EmbeddingDimensionMismatch(RuntimeError):
    """Provider returned vectors whose length differs from the configured dim.

    Deliberately never retried and never let through: a wrong-dim vector either
    poisons the index or dies at the SQL layer where the error gets swallowed
    (vector search silently degrades to keyword-only). Fail loudly here instead."""


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
        self._is_dashscope = "dashscope" in cfg.base_url.lower() or bool(
            _DASHSCOPE_MODEL.match(cfg.model)
        )
        self._send_dimensions = cfg.dim > 0 and bool(_DIMENSIONS_MODEL.match(cfg.model))
        # Flipped once a 400 disappears after dropping our optional fields: the endpoint
        # rejects them, so stop sending (avoids a doubled request on every batch).
        self._extras_unsupported = False
        # LRU of query embeddings (deterministic for a fixed model). Lives with the client,
        # which the factory recreates on any config/model change — so it can't go stale.
        # Keyed by (text_type, text): asymmetric providers emit different vectors per type.
        self._qcache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            timeout=httpx.Timeout(60.0, connect=10.0),
            # Sporadic chat traffic + httpx's 5s default keep-alive = a cold reconnect on almost
            # every turn (added latency, occasional cold-connect failure → vector degrades to
            # keyword-only). Hold connections open longer so turns reuse a warm one.
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=90.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        """Embed any number of texts, transparently chunked under the provider's per-request
        cap. A long knowledge item (>10 chunks) used to 400 the whole call → stored
        unembedded → invisible to vector search; batching fixes that.

        `text_type` ('document'|'query') enables asymmetric retrieval where supported
        (DashScope text-embedding-v*); other providers never see the field."""
        if not texts:
            return []
        bs = max(1, self.cfg.batch_size)
        if len(texts) <= bs:
            return await self._embed_batch(texts, text_type)
        out: list[list[float]] = []
        for i in range(0, len(texts), bs):
            out.extend(await self._embed_batch(texts[i : i + bs], text_type))
        return out

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=6),
        reraise=True,
        # Dim mismatch is a config error — retrying re-buys the same wrong vectors.
        retry=retry_if_not_exception_type(EmbeddingDimensionMismatch),
    )
    async def _embed_batch(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        body: dict = {"model": self.cfg.model, "input": texts}
        extras: dict = {}
        if not self._extras_unsupported:
            if self._send_dimensions:
                # Pin the output width instead of trusting the model default to happen to
                # match the DB column (text-embedding-v4 can emit anything in 64..2048).
                extras["dimensions"] = self.cfg.dim
            if self._is_dashscope:
                extras["text_type"] = text_type
        resp = await self._client.post("/embeddings", json={**body, **extras})
        if extras and resp.status_code == 400:
            # Compatibility fallback: some gateways/models reject optional fields. Retry
            # bare (symmetric embedding, provider-default dim) rather than lose the call;
            # the dim check below still blocks wrong-width vectors.
            log.warning(
                "embeddings_optional_fields_rejected",
                extras=sorted(extras),
                model=self.cfg.model,
                detail=resp.text[:200],
            )
            resp = await self._client.post("/embeddings", json=body)
            if resp.status_code < 400:
                self._extras_unsupported = True
        resp.raise_for_status()
        data = resp.json()["data"]
        # Preserve request order (some providers return out of order).
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [d["embedding"] for d in ordered]
        if self.cfg.dim > 0:
            for v in vectors:
                if len(v) != self.cfg.dim:
                    log.error(
                        "embedding_dim_mismatch",
                        expected=self.cfg.dim,
                        got=len(v),
                        model=self.cfg.model,
                    )
                    raise EmbeddingDimensionMismatch(
                        f"model {self.cfg.model!r} returned {len(v)}-dim vectors but the "
                        f"config expects {self.cfg.dim} — fix EMBEDDING_DIM / the AI config"
                    )
        return vectors

    async def embed_one(self, text: str, text_type: str = "document") -> list[float]:
        """Embed a single query, cached. Queries (search + semantic-cache lookup) repeat far
        more than documents, so this trims a network round-trip + cost off the hot path."""
        key = (text or "").strip()
        if not key:
            return []
        ckey = (text_type, key)
        cached = self._qcache.get(ckey)
        if cached is not None:
            self._qcache.move_to_end(ckey)
            return cached
        try:
            out = await self.embed([key], text_type)
        except EmbeddingDimensionMismatch:
            return []  # already logged at error level; callers degrade to keyword-only
        vec = out[0] if out else []
        if vec:
            self._qcache[ckey] = vec
            self._qcache.move_to_end(ckey)
            if len(self._qcache) > _QCACHE_MAX:
                self._qcache.popitem(last=False)
        return vec
