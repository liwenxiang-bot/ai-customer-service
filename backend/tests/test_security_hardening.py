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


# ---- client IP cannot be spoofed via X-Forwarded-For ----


async def test_direct_client_cannot_spoof_ip_with_xff():
    """Without a trusted proxy in front, a client-sent X-Forwarded-For is ignored."""
    from fastapi import Request

    from app.api.chat.rest import _ip

    scope = {"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6")], "client": ("203.0.113.9", 5000)}
    assert _ip(Request(scope)) == "203.0.113.9"


async def test_trusted_proxy_chain_resolves_real_client():
    """Deployment contract: behind nginx (docker gateway, trusted CIDR) uvicorn resolves the
    right-most untrusted hop, so a forged left-most XFF entry is ignored."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def inner(scope, receive, send):
        seen["client"] = scope["client"][0]

    mw = ProxyHeadersMiddleware(inner, trusted_hosts="127.0.0.1,172.16.0.0/12")
    scope = {
        "type": "http",
        "client": ("172.18.0.1", 40000),
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.9")],
    }
    await mw(scope, None, None)
    assert seen["client"] == "203.0.113.9"


# ---- outbound URL guard (SSRF) ----


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://x", "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/latest/meta-data/", "http://metadata.google.internal/",
])
def test_outbound_url_blocks_metadata_and_odd_schemes(url):
    from app.core.net import UnsafeURL, check_outbound_url

    with pytest.raises(UnsafeURL):
        check_outbound_url(url)


@pytest.mark.parametrize("url", ["http://localhost:8100/v1", "http://192.168.1.20:11434/v1", "https://api.openai.com/v1"])
def test_outbound_url_allows_local_models_and_public_apis(url):
    from app.core.net import check_outbound_url

    check_outbound_url(url)  # self-hosted local / LAN model servers are legitimate


# ---- account password policy ----


def test_account_password_must_be_8_chars():
    from pydantic import ValidationError

    from app.api.admin.accounts import UserCreate, UserUpdate

    with pytest.raises(ValidationError):
        UserCreate(email="a@b.co", name="a", password="")
    with pytest.raises(ValidationError):
        UserUpdate(password="short")
    assert UserCreate(email="a@b.co", name="a", password="longenough").password


# ---- media proxy & security headers ----


async def test_media_documents_download_and_never_sniff(monkeypatch):
    import app.api.chat.rest as rest_mod

    async def fake_fetch(key):
        return (b"%PDF-1.4", "application/pdf") if key.endswith(".pdf") else (b"\x89PNG", "image/png")

    monkeypatch.setattr(rest_mod.storage, "fetch_object", fake_fetch)
    async with await _client() as c:
        pdf = await c.get("/api/chat/media/chat/x/a.pdf")
        png = await c.get("/api/chat/media/chat/x/a.png")
    assert pdf.headers["content-disposition"] == "attachment"
    assert pdf.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in png.headers  # images still render inline


async def test_security_headers_agent_unframable_chat_frameable():
    async with await _client() as c:
        chat = await c.get("/chat")
        health = await c.get("/health/live")
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "x-frame-options" not in chat.headers  # merchants may iframe /chat
    from app.main import _security_headers

    class R:
        headers: dict = {}

    r = R()
    r.headers = {}
    _security_headers("/agent/", r)
    assert r.headers["X-Frame-Options"] == "DENY"


# ---- rate limit window is atomic ----


async def test_rate_limit_key_always_gets_a_ttl(fresh_redis):
    key = f"rl:test:{uuid.uuid4().hex}"
    ok, _ = await rl._check(key, 5, window_sec=30)
    assert ok and 0 < await fresh_redis.ttl(key) <= 30
    await fresh_redis.delete(key)
