"""Regression tests for the audit "day 1" hardening batch.

- /chat must not reflect channel_key into its inline <script> unescaped.
- Production must refuse to boot with placeholder secrets.
- Admin login locks an email out after repeated failures (Redis-backed).
"""

import uuid

import httpx
import pytest
import redis.asyncio as aioredis

import app.api.admin.auth as auth_mod
import app.core.ratelimit as rl
from app.config import Settings, settings
from app.db.session import get_db
from app.main import app
from app.services.auth import AuthError


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---- /chat reflected XSS ----


async def test_chat_page_rejects_script_breaking_channel_key():
    async with await _client() as c:
        for bad in ['x";alert(1);//', "</script><script>alert(1)</script>", "a" * 65, ""]:
            r = await c.get("/chat", params={"channel_key": bad})
            assert r.status_code == 400, bad
            assert "alert" not in r.text


async def test_chat_page_embeds_valid_channel_key_as_json():
    async with await _client() as c:
        r = await c.get("/chat", params={"channel_key": "shop-01"})
    assert r.status_code == 200
    assert 'window.ACS_CONFIG = {"channelKey": "shop-01", "mode": "fullscreen"};' in r.text


# ---- placeholder secrets ----


def test_production_refuses_placeholder_secrets():
    with pytest.raises(RuntimeError):
        Settings(_env_file=None, app_env="production").enforce_secure_defaults()


def test_production_accepts_strong_secrets():
    Settings(
        _env_file=None,
        app_env="production",
        app_secret_key="k" * 64,
        jwt_secret="j" * 64,
        bootstrap_admin_password="not-the-default-1",
    ).enforce_secure_defaults()


def test_development_only_warns_on_placeholder_secrets():
    s = Settings(_env_file=None, app_env="development")
    assert s.insecure_defaults()
    s.enforce_secure_defaults()  # must not raise


# ---- admin login lockout ----


@pytest.fixture
async def fresh_redis(monkeypatch):
    """A Redis client bound to this test's event loop (the app-wide one may be bound to
    another loop from an earlier test)."""
    r = aioredis.from_url(settings.redis_dsn, decode_responses=True)
    try:
        await r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis not available")
    monkeypatch.setattr(rl, "get_redis", lambda: r)
    yield r
    await r.aclose()


async def test_login_locks_email_after_repeated_failures(monkeypatch, fresh_redis):
    async def _fail(db, email, password):
        raise AuthError("邮箱或密码错误")

    async def _no_tenant(db, email):
        return None

    async def _no_db():
        yield None

    monkeypatch.setattr(auth_mod, "authenticate", _fail)
    monkeypatch.setattr(auth_mod, "tenant_for_admin_email", _no_tenant)
    app.dependency_overrides[get_db] = _no_db
    email = f"lockout-{uuid.uuid4().hex[:8]}@example.com"
    try:
        async with await _client() as c:
            codes = [
                (await c.post("/api/admin/auth/login", json={"email": email, "password": "x"}))
                .status_code
                for _ in range(rl.LOGIN_MAX_FAILS_PER_EMAIL + 1)
            ]
        assert codes[: rl.LOGIN_MAX_FAILS_PER_EMAIL] == [401] * rl.LOGIN_MAX_FAILS_PER_EMAIL
        assert codes[-1] == 429
        assert await fresh_redis.ttl(f"login:fail:email:{email}") > 0
    finally:
        app.dependency_overrides.pop(get_db, None)
        await fresh_redis.delete(f"login:fail:email:{email}", "login:fail:ip:127.0.0.1")


async def test_login_success_clears_failure_counter(fresh_redis):
    email = f"clear-{uuid.uuid4().hex[:8]}@example.com"
    await rl.record_login_failure(email, "")
    assert await fresh_redis.get(f"login:fail:email:{email}") == "1"
    await rl.clear_login_failures(email)
    assert await fresh_redis.get(f"login:fail:email:{email}") is None


# ---- demo tools ----


def test_get_order_stub_not_registered_by_default(monkeypatch):
    from app.agent.tools import build_default_registry

    monkeypatch.setattr(settings, "enable_demo_tools", False)
    names = set(build_default_registry().names())
    assert "get_order" not in names
    assert {"search_knowledge", "escalate_to_human"} <= names

    monkeypatch.setattr(settings, "enable_demo_tools", True)
    names = set(build_default_registry().names())
    assert "get_order" in names
