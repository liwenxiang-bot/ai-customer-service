"""Reliability / ops fixes: queue DSN, single-flight rebuilds, stale-job sweep, attachment purge,
cached health, bounded metric labels."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.db.session import drain_after_commit_tasks
from app.models.enums import JobStatus
from app.models.knowledge import EmbeddingRebuildJob


def test_queue_uses_the_same_redis_as_the_api(monkeypatch):
    from app.tasks import queue

    monkeypatch.setattr(settings, "redis_url", "redis://cache.internal:6380/3")
    rs = queue.redis_settings()
    assert (rs.host, rs.port, rs.database) == ("cache.internal", 6380, 3)


@pytest.fixture
async def db():
    engine = create_async_engine(settings.app_sqlalchemy_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("postgres not available")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s
    await engine.dispose()


async def test_rebuild_is_single_flight_and_enqueued_after_commit(db, _no_real_job_queue):
    from app.services.embedding_rebuild import start_rebuild

    model = f"m-{uuid.uuid4().hex[:6]}"
    j1 = await start_rebuild(db, "old", settings.embedding_dim, model, settings.embedding_dim)
    assert _no_real_job_queue.jobs == []  # nothing before commit
    await db.commit()
    await drain_after_commit_tasks()
    assert [j[0] for j in _no_real_job_queue.jobs] == ["rebuild_embeddings"]
    j2 = await start_rebuild(db, "old", settings.embedding_dim, model, settings.embedding_dim)
    assert j2.id == j1.id  # reused while pending
    await db.delete(j1)
    await db.commit()


async def test_stale_running_job_is_marked_failed(db):
    from app.services.embedding_rebuild import fail_stale_jobs

    job = EmbeddingRebuildJob(status=JobStatus.RUNNING, to_model="x", to_dim=1)
    db.add(job)
    await db.flush()
    job.created_at = datetime.now(UTC) - timedelta(hours=4)
    await db.flush()
    assert await fail_stale_jobs(db) >= 1
    await db.refresh(job)
    assert job.status == JobStatus.FAILED and "重新发起" in job.error
    await db.delete(job)
    await db.commit()


async def test_user_deletion_also_removes_stored_attachments(db, monkeypatch):
    from app.core import storage
    from app.models.conversation import Message, Session
    from app.models.enums import MessageRole, SessionStatus
    from app.services.cleanup import delete_user_data

    deleted = []

    async def fake_delete(keys):
        deleted.extend(keys)
        return len(keys)

    monkeypatch.setattr(storage, "delete_objects", fake_delete)
    uid = f"u-{uuid.uuid4().hex[:8]}"
    s = Session(channel_type="web", channel_key="default", end_user_id=uid,
                status=SessionStatus.CLOSED, last_activity_at=datetime.now(UTC))
    db.add(s)
    await db.flush()
    db.add_all([
        Message(tenant_id=s.tenant_id, session_id=s.id, seq=1, role=MessageRole.USER, content="",
                attachments=[{"key": "chat/default/a.png"}]),
        Message(tenant_id=s.tenant_id, session_id=s.id, seq=2, role=MessageRole.ASSISTANT, model="human",
                content="", attachments=[{"key": f"agent/{s.id}/b.pdf"}]),
    ])
    await db.commit()
    assert await delete_user_data(db, uid) == 1
    assert sorted(deleted) == sorted(["chat/default/a.png", f"agent/{s.id}/b.pdf"])


async def test_full_health_is_cached(monkeypatch):
    from app.core import health

    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(health, "_probe", probe)
    monkeypatch.setattr(health, "_health_cache", None)
    await health.full_health()
    await health.full_health()
    assert calls["n"] == 1


async def test_metrics_use_route_templates_not_raw_paths():
    import httpx
    from prometheus_client import REGISTRY

    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.get(f"/api/chat/media/chat/x/{uuid.uuid4().hex}.png")
    paths = {s.labels.get("path") for m in REGISTRY.collect() if m.name == "acs_http_requests" for s in m.samples}
    assert "/api/chat/media/{key:path}" in paths
    assert not any(p and p.startswith("/api/chat/media/chat/") for p in paths)
