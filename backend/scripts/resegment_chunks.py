"""Backfill knowledge_chunks.content_seg with jieba-segmented content (no re-embedding).

Run once after migration 0003 to activate Chinese full-text search for EXISTING
knowledge without paying for embeddings again:

    python -m scripts.resegment_chunks

New / edited knowledge is segmented automatically by reembed_item; this is only for
rows that already existed before the upgrade.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeChunk
from app.rag.segment import available, segment

log = get_logger("resegment")


async def main() -> None:
    configure_logging()
    if not available():
        print("jieba unavailable — `pip install jieba` before resegmenting.")
        return
    async with session_scope() as db:
        chunks = (await db.execute(select(KnowledgeChunk))).scalars().all()
        changed = 0
        for c in chunks:
            seg = segment(c.content)
            if seg != c.content_seg:
                c.content_seg = seg
                changed += 1
        await db.commit()
        print(f"Resegmented {changed}/{len(chunks)} chunks.")


if __name__ == "__main__":
    asyncio.run(main())
