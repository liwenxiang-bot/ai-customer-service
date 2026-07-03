"""Tiny OpenAI-compatible embedding server backed by fastembed (ONNX, no PyTorch).

Serves /v1/embeddings so the main app can use a free, local, Chinese-optimized
embedding model (BAAI/bge-small-zh-v1.5, 512-dim). Data never leaves the machine —
a good fit for customer-service privacy. Run:

    python -m scripts.local_embedding_server          # port 8100

Then point the admin AI config (or .env) at  http://localhost:8100/v1.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import FastAPI
from fastembed import TextEmbedding
from pydantic import BaseModel

log = logging.getLogger("local_embedding_server")

MODEL_NAME = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

# BGE-zh retrieval convention: queries (not documents) carry this instruction prefix so
# short questions land near the long passages that answer them. fastembed's query_embed()
# is a no-op for this model, so the prefix is applied here by hand.
_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# bge-small-zh-v1.5 caps at 512 tokens and fastembed truncates silently past that; for
# Chinese ~1 char ≈ 1 token, so a character count is a good-enough proxy (with margin for
# tokenizer overhead and the query prefix). Beyond this the tail never reaches the vector.
_MAX_SEQ_CHARS = 500

app = FastAPI(title="Local Embedding Server")
_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    # Asymmetric retrieval marker ('query' | 'document'); not part of the OpenAI schema,
    # so it stays optional and absent means document behaviour (the safe default).
    text_type: str | None = None


@app.post("/v1/embeddings")
def embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    if req.text_type == "query":
        texts = [_QUERY_INSTRUCTION + t for t in texts]
    over_limit = sum(1 for t in texts if len(t) > _MAX_SEQ_CHARS)
    if over_limit:
        log.warning(
            "%d/%d input(s) exceed ~%d chars (%s max_seq_length is 512 tokens); their tails "
            "are silently dropped from the vectors — lower rag chunk_size for local mode",
            over_limit,
            len(texts),
            _MAX_SEQ_CHARS,
            MODEL_NAME,
        )
    vectors = [v.tolist() for v in _get_model().embed(texts)]
    return {
        "object": "list",
        "model": req.model or MODEL_NAME,
        "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)},
    }


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME}


if __name__ == "__main__":
    _get_model()  # warm up (downloads the model on first run)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LOCAL_EMBED_PORT", "8100")))
