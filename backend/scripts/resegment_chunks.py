"""Backfill knowledge_chunks.content_seg with jieba-segmented, title-anchored content
(no re-embedding).

Run once after migration 0003 (Chinese FTS) and again after the title-anchor change:
every chunk's FTS input now carries its item's title and tags, so heading-style
queries ("退货政策") can reach chunks deep in a long document without paying for
embeddings again:

    python -m scripts.resegment_chunks

This fixes the KEYWORD path only. The vector side of the anchor change (title prefixed
into the embedding input) requires a re-embed — trigger a rebuild from the admin, or
let edits re-embed items organically. New / edited knowledge is anchored automatically
by reembed_item; this script is only for rows written before the upgrade.

Note: chunks written before the anchor change have the title inside chunk 0's content
(the old scheme concatenated title+content before splitting). Anchoring the seg input
double-counts the title there, which is harmless for tsvector matching.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope
from app.models.knowledge import KnowledgeChunk, KnowledgeItem
from app.rag.segment import available, segment

log = get_logger("resegment")


def _fts_input(item: KnowledgeItem | None, content: str) -> str:
    if item is None:
        return content
    anchor = (item.title or "").strip()
    tags_line = " ".join(str(t) for t in (item.tags or []) if t).strip()
    head = "\n".join(p for p in (anchor, tags_line) if p)
    return f"{head}\n{content}" if head else content


async def main() -> None:
    configure_logging()
    if not available():
        print("jieba unavailable — `pip install jieba` before resegmenting.")
        return
    async with session_scope() as db:
        items = {i.id: i for i in (await db.execute(select(KnowledgeItem))).scalars()}
        chunks = (await db.execute(select(KnowledgeChunk))).scalars().all()
        changed = 0
        for c in chunks:
            seg = segment(_fts_input(items.get(c.item_id), c.content))
            if seg != c.content_seg:
                c.content_seg = seg
                changed += 1
        await db.commit()
        print(f"Resegmented {changed}/{len(chunks)} chunks.")


if __name__ == "__main__":
    asyncio.run(main())
