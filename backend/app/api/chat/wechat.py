"""WeChat Work callback endpoint.

GET  → URL verification handshake (echo decrypted echostr).
POST → receive an (encrypted) message: verify + decrypt, ACK immediately, then process
the turn in the background and push the reply via the WeChat send API. ACK-first keeps
the callback well under WeChat's timeout (requirements §5.2, §14).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse

from app.channels.wechat import (
    build_crypto,
    inbound_from_wechat,
    load_wechat_config,
    parse_inbound,
)
from app.channels.wechat_crypto import WeChatCryptoError
from app.core.logging import get_logger
from app.db.session import session_scope
from app.services.conversation import handle_turn
from app.services.wechat_client import send_text

router = APIRouter(prefix="/api/wechat", tags=["wechat"])
log = get_logger("wechat.callback")


@router.get("/callback")
async def verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
):
    async with session_scope() as db:
        cfg = await load_wechat_config(db)
    if not cfg or not cfg.encoding_aes_key:
        return PlainTextResponse("not configured", status_code=400)
    try:
        crypto = build_crypto(cfg)
        decrypted = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
        return PlainTextResponse(decrypted)
    except WeChatCryptoError as exc:
        log.warning("wechat_verify_failed", error=str(exc))
        return PlainTextResponse("verify failed", status_code=403)


@router.post("/callback")
async def receive(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    body = (await request.body()).decode("utf-8")
    async with session_scope() as db:
        cfg = await load_wechat_config(db)
    if not cfg or not cfg.enabled:
        return PlainTextResponse("")

    try:
        crypto = build_crypto(cfg)
        encrypt = _extract_encrypt(body)
        plain = crypto.decrypt_message(encrypt, msg_signature, timestamp, nonce)
        msg = parse_inbound(plain)
    except (WeChatCryptoError, Exception) as exc:  # noqa: BLE001
        log.warning("wechat_decrypt_failed", error=str(exc))
        return PlainTextResponse("")

    inbound = inbound_from_wechat(msg)
    if inbound is not None:
        # ACK now; process + push asynchronously so we never hit WeChat's callback timeout.
        asyncio.create_task(_process_and_push(cfg, inbound))

    # Empty 200 is a valid ACK for WeChat Work app callbacks.
    return PlainTextResponse("")


async def _process_and_push(cfg, inbound) -> None:
    try:
        parts: list[str] = []
        async with session_scope() as db:
            async for ev in handle_turn(db, inbound):
                if ev.kind == "text":
                    parts.append(ev.text)
        reply = "".join(parts).strip() or "（暂无回复）"
        await send_text(cfg.corp_id, cfg.secret, cfg.agent_id, inbound.end_user_id, reply)
    except Exception as exc:  # noqa: BLE001
        log.warning("wechat_process_failed", error=str(exc))


def _extract_encrypt(xml_body: str) -> str:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_body)
    node = root.find("Encrypt")
    if node is None or not node.text:
        raise WeChatCryptoError("no Encrypt node")
    return node.text
