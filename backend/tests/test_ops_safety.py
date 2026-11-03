"""Configuration guards that keep a deployment from being put into a broken / unsafe state."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.admin.ai_config as aicfg
import app.api.admin.tenants as tenants_api
import app.services.channel as channel_svc
from app.config import settings
from app.db.tenant_context import get_current_tenant, tenant_scope


# ---- embedding dimension must match the deployed vector columns ----


async def test_changing_embedding_dim_away_from_deployment_is_refused(monkeypatch):
    cfg = SimpleNamespace(embedding_model="text-embedding-v4", embedding_dim=settings.embedding_dim)

    async def active(db):
        return cfg

    monkeypatch.setattr(aicfg, "get_active_ai_config", active)
    body = aicfg.AIConfigUpdate(embedding_dim=settings.embedding_dim + 512, confirm_rebuild=True)
    with pytest.raises(HTTPException) as e:
        await aicfg.update_config(body, db=None, user=None)
    assert e.value.status_code == 409 and "EMBEDDING_DIM" in e.value.detail
    assert cfg.embedding_dim == settings.embedding_dim  # nothing was mutated


# ---- REST callers: the channel's tenant is pinned for the rest of the request ----


class _RecDB:
    def __init__(self):
        self.sql = []

    async def execute(self, stmt, params=None):
        self.sql.append((str(stmt), params))


async def test_rest_channel_resolution_switches_and_repins_tenant(monkeypatch):
    other = uuid.uuid4()

    async def tenant_for_channel(db, kind, key):
        return other

    async def get_web_channel(db, key):
        return SimpleNamespace(key=key, tenant=get_current_tenant())

    monkeypatch.setattr("app.services.tenant.tenant_for_channel", tenant_for_channel)
    monkeypatch.setattr(channel_svc, "get_web_channel", get_web_channel)
    db = _RecDB()
    with tenant_scope(uuid.UUID("00000000-0000-0000-0000-000000000001")):
        ch = await channel_svc.resolve_active_web_channel(db, "shop-b")
        assert get_current_tenant() == other
    assert ch.tenant == other  # the channel lookup ran under the channel's tenant
    assert any("set_config('app.tenant_id'" in s and p == {"t": str(other)} for s, p in db.sql)


# ---- no second tenant while RLS is bypassed ----


@pytest.fixture
async def real_db():
    engine = create_async_engine(settings.app_sqlalchemy_url)
    try:
        async with engine.connect():
            pass
    except Exception:  # noqa: BLE001
        await engine.dispose()
        pytest.skip("postgres not available")
    async with AsyncSession(engine) as s:
        yield s
    await engine.dispose()


async def test_rls_bypass_detection_against_real_role(real_db):
    # The dev/CI role is the owner superuser (single-tenant default) → bypasses RLS.
    assert await tenants_api.rls_bypassed(real_db) is True


async def test_tenant_creation_refused_while_rls_is_bypassed(monkeypatch):
    async def bypassed(db):
        return True

    async def must_not_provision(*a, **k):
        raise AssertionError("must not provision")

    monkeypatch.setattr(tenants_api, "rls_bypassed", bypassed)
    monkeypatch.setattr(tenants_api.tsvc, "provision_tenant", must_not_provision)
    body = tenants_api.CreateTenantIn(name="B", slug="b", admin_email="b@x.com", admin_password="longenough1")
    with pytest.raises(HTTPException) as e:
        await tenants_api.create_tenant(body, db=None, user=None)
    assert e.value.status_code == 409 and "RLS" in e.value.detail
