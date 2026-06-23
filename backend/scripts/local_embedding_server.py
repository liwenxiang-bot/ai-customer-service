"""Tiny OpenAI-compatible embedding server backed by fastembed (ONNX, no PyTorch).

Serves /v1/embeddings so the main app can use a free, local, Chinese-optimized
embedding model (BAAI/bge-small-zh-v1.5, 512-dim). Data never leaves the machine —
a good fit for customer-service privacy. Run:

    python -m scripts.local_embedding_server          # port 8100

Then point the admin AI config (or .env) at  http://localhost:8100/v1.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastembed import TextEmbedding
from pydantic import BaseModel

MODEL_NAME = os.environ.get("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

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


@app.post("/v1/embeddings")
def embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
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
