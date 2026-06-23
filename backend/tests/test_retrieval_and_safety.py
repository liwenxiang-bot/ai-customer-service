"""Unit: RRF fusion, pricing, and content safety filter."""

import pytest

from app.llm.pricing import estimate_cost, price_for
from app.models.config import AIConfig
from app.rag.retrieval import _fuse
from app.services.content_safety import check_input


def test_fuse_combines_and_ranks():
    # (item_id, chunk_id, title, content)
    v = [("i1", "c1", "t1", "x"), ("i2", "c2", "t2", "y")]
    k = [("i2", "c2", "t2", "y"), ("i3", "c3", "t3", "z")]
    fused = _fuse(v, k, vw=0.6, kw=0.4)
    ids = [f[1] for f in fused]
    # c2 appears in both lists → should rank first.
    assert ids[0] == "c2"
    assert set(ids) == {"c1", "c2", "c3"}


def test_fuse_empty():
    assert _fuse([], [], 0.6, 0.4) == []


def test_pricing_known_and_unknown():
    assert price_for("gpt-4o-mini") == (0.15, 0.60)
    assert price_for("some-unknown-model") == (1.00, 3.00)
    cost = estimate_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert round(cost, 2) == 0.75


@pytest.mark.asyncio
async def test_content_safety_disabled_passes():
    cfg = AIConfig(content_safety_enabled=False)
    ok, _ = await check_input(cfg, "任何内容")
    assert ok is True


@pytest.mark.asyncio
async def test_content_safety_enabled_no_blocklist_passes():
    # Default blocklist is empty → nothing blocked even when enabled.
    cfg = AIConfig(content_safety_enabled=True)
    ok, _ = await check_input(cfg, "正常的问题")
    assert ok is True
