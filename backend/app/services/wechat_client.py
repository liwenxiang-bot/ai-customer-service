"""WeChat Work (企业微信) API client: cached access_token + outbound message send."""

from __future__ import annotations

import re

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


# WeChat Work text messages are capped at 2048 *bytes*; keep headroom for safety.
_MAX_TEXT_BYTES = 2000
# access_token invalid / expired → refresh once and retry.
_TOKEN_ERRCODES = {40014, 42001}


def to_plain_text(md: str) -> str:
    """WeChat Work text messages don't render Markdown: strip the common markup the LLM emits
    so customers don't see raw `**`, `#` or `[text](url)`."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 \2", md)  # [t](u) → t u
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)            # headings
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)                       # bold
    text = re.sub(r"`{1,3}", "", text)                                      # code ticks
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)                  # bullets
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_for_wechat(text: str, limit: int = _MAX_TEXT_BYTES) -> list[str]:
    """Split into chunks of at most `limit` UTF-8 bytes, preferring paragraph/line breaks."""
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate.encode()) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
        buf = ""
        while len(line.encode()) > limit:  # a single over-long line: hard-cut on characters
            cut = len(line)
            while len(line[:cut].encode()) > limit:
                cut = cut * limit // len(line[:cut].encode()) or cut - 1
            chunks.append(line[:cut])
            line = line[cut:]
        buf = line
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


async def _invalidate_token(corp_id: str) -> None:
    try:
        await get_redis().delete(f"wechat:token:{corp_id}")
    except Exception:  # noqa: BLE001
        pass


async def _send_one(corp_id: str, secret: str, agent_id: str, touser: str, content: str) -> bool:
    for attempt in (1, 2):
        token = await get_access_token(corp_id, secret)
        if not token:
            return False
        payload = {
            "touser": touser,
            "msgtype": "text",
            "agentid": int(agent_id) if str(agent_id).isdigit() else agent_id,
            "text": {"content": content},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{_BASE}/message/send", params={"access_token": token}, json=payload
                )
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("wechat_send_error", error=str(exc))
            return False
        code = data.get("errcode", 0)
        if code == 0:
            return True
        if code in _TOKEN_ERRCODES and attempt == 1:
            await _invalidate_token(corp_id)
            continue
        log.warning("wechat_send_failed", errcode=code, errmsg=data.get("errmsg"))
        return False
    return False


async def send_text(corp_id: str, secret: str, agent_id: str, touser: str, content: str) -> bool:
    """Send a (possibly long, possibly Markdown) reply as one or more plain-text messages.
    Returns False as soon as one chunk fails (the caller retries the whole send)."""
    for chunk in split_for_wechat(to_plain_text(content)):
        if not await _send_one(corp_id, secret, agent_id, touser, chunk):
            return False
    return True
