"""WeChat Work (企业微信) API client: cached access_token + outbound message send."""

from __future__ import annotations

import httpx

from app.core.logging import get_logger
from app.core.redis_client import get_redis

log = get_logger("wechat.client")

_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


async def get_access_token(corp_id: str, secret: str) -> str | None:
    """Fetch (and cache in Redis) an access_token for the corp+secret."""
    cache_key = f"wechat:token:{corp_id}"
    r = get_redis()
    cached = await r.get(cache_key)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE}/gettoken", params={"corpid": corp_id, "corpsecret": secret})
            data = resp.json()
        if data.get("errcode", 0) != 0:
            log.warning("wechat_token_failed", errcode=data.get("errcode"), errmsg=data.get("errmsg"))
            return None
        token = data["access_token"]
        # Cache slightly under the real TTL (default 7200s).
        await r.set(cache_key, token, ex=max(data.get("expires_in", 7200) - 200, 60))
        return token
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("wechat_token_error", error=str(exc))
        return None


async def send_text(corp_id: str, secret: str, agent_id: str, touser: str, content: str) -> bool:
    token = await get_access_token(corp_id, secret)
    if not token:
        return False
    payload = {
        "touser": touser,
        "msgtype": "text",
        "agentid": int(agent_id) if str(agent_id).isdigit() else agent_id,
        "text": {"content": content[:2000]},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{_BASE}/message/send", params={"access_token": token}, json=payload)
            data = resp.json()
        if data.get("errcode", 0) != 0:
            log.warning("wechat_send_failed", errcode=data.get("errcode"), errmsg=data.get("errmsg"))
            return False
        return True
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("wechat_send_error", error=str(exc))
        return False
