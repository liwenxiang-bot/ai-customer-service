"""WeChat Work callback endpoint.

GET  → URL verification handshake (echo decrypted echostr).
POST → receive an (encrypted) message: verify + decrypt, ACK immediately, then process
the turn in the background and push the reply via the WeChat send API. ACK-first keeps
the callback well under WeChat's timeout (requirements §5.2, §14).
"""

from __future__ import annotations

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
from app.tasks.queue import enqueue

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

    if inbound_from_wechat(msg) is not None:
        # ACK now and hand the turn to the worker: a durable job survives deploys/restarts
        # (an in-process task did not), and keying it by MsgId drops WeChat's retries of the
        # same message (it re-posts when it doesn't get a timely ACK).
        job_key = msg.get("MsgId") or f"{msg.get('FromUserName', '')}:{msg.get('CreateTime', '')}"
        await enqueue("wechat_handle_inbound", msg, _job_id=f"wx-in:{job_key}")

    # Empty 200 is a valid ACK for WeChat Work app callbacks.
    return PlainTextResponse("")


def _extract_encrypt(xml_body: str) -> str:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_body)
    node = root.find("Encrypt")
    if node is None or not node.text:
        raise WeChatCryptoError("no Encrypt node")
    return node.text
