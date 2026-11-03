"""Shared test fixtures."""

import pytest

import app.tasks.queue as queue_mod


class _RecordingPool:
    """Stands in for the ARQ pool: tests must never push real jobs into the dev Redis — a
    running worker would later execute them against rows the tests already deleted."""

    def __init__(self):
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))
        return None


@pytest.fixture(autouse=True)
def _no_real_job_queue(monkeypatch):
    pool = _RecordingPool()

    async def _get_pool():
        return pool

    monkeypatch.setattr(queue_mod, "get_pool", _get_pool)
    return pool


@pytest.fixture(autouse=True)
def _fresh_redis_client():
    """The app keeps one global Redis client bound to the event loop that created it; pytest
    gives every test its own loop. Drop the cached client after each test so the next one
    builds a client on its own loop (instead of 'Future attached to a different loop')."""
    import app.core.redis_client as rc

    yield
    rc._pool = None


@pytest.fixture(autouse=True)
def _no_dashboard_counters(monkeypatch):
    """Guard-block tallies feed the live dashboard: tests must not write them to the dev Redis."""

    async def _noop(kind):
        return None

    monkeypatch.setattr("app.services.abuse_guard.count_block", _noop)
    monkeypatch.setattr("app.agent.runner.count_block", _noop)
