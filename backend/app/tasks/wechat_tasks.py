"""WeChat Work background jobs.

The callback endpoint only verifies, decrypts and enqueues (keyed by MsgId, so WeChat's
retries of the same message are dropped); the turn and the outbound push run here, where a
worker restart re-runs the job instead of silently losing the customer's message.
"""

from __future__ import annotations

from arq import Retry

from app.channels.wechat import inbound_from_wechat, load_wechat_config
from app.core.logging import get_logger
from app.db.session import session_scope
from app.db.tenant_context import tenant_scope

log = get_logger("tasks.wechat")

SEND_MAX_TRIES = 4


async def wechat_handle_inbound(ctx, msg: dict, _tenant: str | None = None) -> str:
    """Run one customer turn for a decrypted WeChat message and queue the reply."""
    from app.services.conversation import handle_turn
    from app.tasks.queue import enqueue

    inbound = inbound_from_wechat(msg)
    if inbound is None:
        return "ignored"
    with tenant_scope(_tenant):
        parts: list[str] = []
        final: dict = {}
        async with session_scope() as db:
            async for ev in handle_turn(db, inbound):
                if ev.kind == "text":
                    parts.append(ev.text)
                elif ev.kind == "done":
                    final = ev.data or {}
        if final.get("takeover"):
            return "takeover"  # AI paused; the operator replies from the workbench
        reply = "".join(parts).strip()
        if not reply:
            return "empty"
        await enqueue("wechat_send", inbound.end_user_id, reply, _tenant=_tenant)
        return "queued"


async def wechat_send(ctx, touser: str, content: str, _tenant: str | None = None) -> bool:
    """Push a message to a WeChat Work user, retrying with backoff on failure."""
    from app.services.wechat_client import send_text

    with tenant_scope(_tenant):
        async with session_scope() as db:
            cfg = await load_wechat_config(db)
    if not cfg or not cfg.enabled or not cfg.secret:
        log.warning("wechat_send_skipped_not_configured", touser=touser)
        return False
    if await send_text(cfg.corp_id, cfg.secret, cfg.agent_id, touser, content):
        return True
    tries = ctx.get("job_try", 1)
    if tries < SEND_MAX_TRIES:
        raise Retry(defer=5 * tries)
    log.error("wechat_send_gave_up", touser=touser, tries=tries)
    return False


async def notify_handoff(ctx, ticket_id: str, _tenant: str | None = None) -> bool:
    """Send the operator notification for a new handoff ticket (after its commit)."""
    import uuid

    from app.models.conversation import HandoffTicket
    from app.services.handoff import send_handoff_notify
    from app.services.notify import NO_CHANNEL_CONFIGURED

    with tenant_scope(_tenant):
        async with session_scope() as db:
            ticket = await db.get(HandoffTicket, uuid.UUID(ticket_id))
            if ticket is None or ticket.notified:
                return bool(ticket and ticket.notified)
            delivered, err = await send_handoff_notify(db, ticket)
    # Nothing configured → retrying can't help; transient webhook/SMTP failures get 2 retries.
    if not delivered and NO_CHANNEL_CONFIGURED not in err and ctx.get("job_try", 1) < 3:
        raise Retry(defer=30)
    return delivered
