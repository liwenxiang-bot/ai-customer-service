"""DB-gated regression: editing a knowledge item must survive post-commit serialization.

`updated_at` is a server-onupdate column: an UPDATE flush expires it, and reading it
back needs IO — under the async session a plain attribute access raises MissingGreenlet
unless the mapper fetches defaults eagerly (TimestampMixin sets eager_defaults=True,
so the UPDATE carries a RETURNING clause). The admin API serializes the row right
after commit, so a break here means "row saved but the request 500s" — the kind of
half-failure that's easy to ship unnoticed. Skips when Postgres is unreachable.
"""

import pytest
from sqlalchemy import text as sql_text

from app.models.enums import KnowledgeStatus


@pytest.fixture
async def pg_db(monkeypatch):
    """Session against the configured Postgres, or skip when it isn't reachable."""
    from app.db.session import SessionLocal

    async def _noop(db, item_id):  # keep tests offline: no Redis enqueue
        return None

    monkeypatch.setattr("app.services.knowledge._enqueue_embed", _noop)

    session = SessionLocal()
    try:
        await session.execute(sql_text("SELECT 1 FROM knowledge_items LIMIT 1"))
    except Exception as exc:  # noqa: BLE001 — unreachable/unmigrated DB
        await session.close()
        pytest.skip(f"Postgres unavailable for the write-path gate: {exc}")
    try:
        yield session
    finally:
        await session.close()


async def test_update_item_survives_post_commit_serialization(pg_db):
    from app.services import knowledge as ksvc

    item = await ksvc.create_item(
        pg_db,
        {
            "title": "[write-path-gate] 临时条目",
            "content": "回归测试正文",
            "status": KnowledgeStatus.DRAFT,  # draft: stays out of retrieval
        },
        None,
    )
    await pg_db.commit()
    try:
        await ksvc.update_item(
            pg_db, item, {"content": "回归测试正文 v2", "change_note": "regression"}, None
        )
        await pg_db.commit()
        # What every admin handler does right after commit — must not raise
        # MissingGreenlet, and must reflect the server-generated value.
        assert item.updated_at is not None
        assert item.version == 2
    finally:
        await ksvc.delete_item(pg_db, item, None)
        await pg_db.commit()
