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
