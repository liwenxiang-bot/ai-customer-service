"""One-off: point the active AI config at the local embedding server (512-dim) and
re-shape the vector columns. Run with EMBEDDING_DIM=512 so the ORM matches.

    EMBEDDING_DIM=512 python -m scripts.switch_to_local_embedding
"""

import asyncio

from sqlalchemy import select

from app.core.encryption import encrypt_secret
from app.db.session import session_scope
from app.models.config import AIConfig
from app.services.embedding_rebuild import _alter_vector_dim


async def main() -> None:
    async with session_scope() as db:
        cfg = (
            await db.execute(select(AIConfig).where(AIConfig.is_active.is_(True)).limit(1))
        ).scalar_one()
        cfg.embedding_base_url = "http://localhost:8100/v1"
        cfg.embedding_api_key_enc = encrypt_secret("local")
        cfg.embedding_model = "bge-small-zh-v1.5"
        cfg.embedding_dim = 512
        await db.flush()
        # Re-shape knowledge_chunks.embedding + semantic_cache.embedding to vector(512),
        # rebuilding the HNSW indexes (the embedding-migration code path).
        await _alter_vector_dim(db, 512)
        print("✅ ai_config → local bge-small-zh-v1.5 (512d); vector columns altered to 512")


if __name__ == "__main__":
    asyncio.run(main())
