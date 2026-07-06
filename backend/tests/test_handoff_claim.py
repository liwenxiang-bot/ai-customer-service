"""Integration: agent claim/unclaim contract + release auto-resolve + queue-counts mine.

契约 A（坐席认领）：
- claim 仅 open 且未分配时成功；同人重复认领幂等 200；已被他人认领 409。
- unclaim 仅本人或 admin 可退回，status→open、assignee 清空。
- release 不论 resume_ai 都自动 resolve 该会话 open/in_progress 的工单——人工介入
  已结束，工单继续挂在坐席名下只会变成幽灵计数（进 mine 角标、不进任何列表）。
- queue-counts 的 mine = 我认领的 in_progress 工单（且会话仍在人工队列）
  + 我接管中的会话（按会话去重）。

契约 B（会话离队必收尾工单，防幽灵计数）：
- 客户结束会话（close_session）/ 标记完成（mark-handled）同样 resolve 存活工单。
- create_handoff 对已有存活工单幂等复用；会话回到 active 后可重新转人工建单。
- 后台 resolve 最后一张存活工单时，escalated 会话回迁 active（不再滞留待认领）。

Requires the dev Postgres (and Redis for the release test); skips otherwise.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.models.admin import AdminUser, AuditLog
from app.models.conversation import HandoffTicket, Session
from app.models.enums import AdminRole, HandoffStatus, SessionStatus


async def _db_up() -> bool:
    try:
        from sqlalchemy import text

        from app.db.session import engine

        # Drop pooled connections bound to a previous test's (closed) event loop.
        await engine.dispose(close=False)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _redis_up() -> bool:
    from app.core.redis_client import close_redis, get_redis

    await close_redis()  # rebind the pool to this test's event loop
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


def _operator(tag: str) -> AdminUser:
    return AdminUser(
        email=f"agent-{tag}-{uuid.uuid4().hex[:8]}@test.local",
        name=f"坐席{tag}",
        password_hash="x",
        role=AdminRole.OPERATOR,
    )


def _session(status: str = SessionStatus.ESCALATED) -> Session:
    return Session(
        channel_type="web", channel_key="default", end_user_id=f"cust-{uuid.uuid4().hex[:8]}",
        status=status, escalated=True, last_activity_at=datetime.now(UTC),
    )


def _ticket(session: Session, status: str = HandoffStatus.OPEN) -> HandoffTicket:
    return HandoffTicket(
        session_id=session.id, channel_type="web", end_user_id=session.end_user_id,
        reason="user_request", status=status,
    )


async def _cleanup(db, sessions, users):
    from sqlalchemy import delete

    sids = [s.id for s in sessions]
    uids = [u.id for u in users]
    await db.execute(delete(HandoffTicket).where(HandoffTicket.session_id.in_(sids)))
    await db.execute(delete(Session).where(Session.id.in_(sids)))
    await db.execute(delete(AuditLog).where(AuditLog.actor_id.in_(uids)))
    await db.execute(delete(AdminUser).where(AdminUser.id.in_(uids)))
    await db.commit()


@pytest.mark.asyncio
async def test_claim_conflict_idempotent_and_unclaim():
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.handoff import claim_ticket, unclaim_ticket
    from app.db.session import session_scope

    async with session_scope() as db:
        op1, op2 = _operator("A"), _operator("B")
        session = _session()
        db.add_all([op1, op2, session])
        await db.flush()
        ticket = _ticket(session)
        db.add(ticket)
        await db.commit()
        tid = str(ticket.id)

        try:
            # First claim wins: status → in_progress, assignee = op1.
            out = await claim_ticket(tid, db=db, user=op1)
            assert out["status"] == HandoffStatus.IN_PROGRESS
            assert out["assignee_id"] == str(op1.id)
            assert out["assignee_name"] == "坐席A"

            # Same operator re-claims → idempotent 200 with the same payload.
            again = await claim_ticket(tid, db=db, user=op1)
            assert again["assignee_id"] == str(op1.id)
            assert again["status"] == HandoffStatus.IN_PROGRESS

            # Another operator → 409 已被 X 认领.
            with pytest.raises(HTTPException) as ei:
                await claim_ticket(tid, db=db, user=op2)
            assert ei.value.status_code == 409
            assert "已被" in ei.value.detail and "坐席A" in ei.value.detail

            # Unclaim by someone else (non-admin) → 403.
            with pytest.raises(HTTPException) as ei:
                await unclaim_ticket(tid, db=db, user=op2)
            assert ei.value.status_code == 403

            # Unclaim by the owner → back to open, assignee cleared.
            back = await unclaim_ticket(tid, db=db, user=op1)
            assert back["status"] == HandoffStatus.OPEN
            assert back["assignee_id"] is None

            # Now op2 can claim it.
            out2 = await claim_ticket(tid, db=db, user=op2)
            assert out2["assignee_id"] == str(op2.id)

            # Audit trail recorded claim + unclaim.
            from sqlalchemy import func, select

            n = (
                await db.execute(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.target_id == tid,
                        AuditLog.action.in_(["handoff.claim", "handoff.unclaim"]),
                    )
                )
            ).scalar_one()
            assert n == 3  # claim(op1) + unclaim(op1) + claim(op2)

            # Resolved tickets can't be claimed.
            ticket.status = HandoffStatus.RESOLVED
            await db.commit()
            with pytest.raises(HTTPException) as ei:
                await claim_ticket(tid, db=db, user=op1)
            assert ei.value.status_code == 409
        finally:
            await _cleanup(db, [session], [op1, op2])


@pytest.mark.asyncio
async def test_release_without_resume_auto_resolves_tickets():
    if not await _db_up():
        pytest.skip("postgres not available")
    if not await _redis_up():
        pytest.skip("redis not available")

    from app.db.session import session_scope
    from app.services import takeover as tk

    async with session_scope() as db:
        op = _operator("R")
        session = _session(status=SessionStatus.HUMAN_TAKEOVER)
        db.add_all([op, session])
        await db.flush()
        t_open = _ticket(session, HandoffStatus.OPEN)
        t_prog = _ticket(session, HandoffStatus.IN_PROGRESS)
        t_prog.assignee_id = op.id
        db.add_all([t_open, t_prog])
        await db.commit()

        try:
            resolved = await tk.end_takeover(db, session, resume_ai=False, operator=op)
            await db.commit()
            assert sorted(resolved) == sorted([str(t_open.id), str(t_prog.id)])
            assert session.status == SessionStatus.HUMAN_HANDLED
            for t in (t_open, t_prog):
                await db.refresh(t)
                assert t.status == HandoffStatus.RESOLVED
                assert t.resolution_note == "坐席结束会话"
                assert t.resolved_at is not None
            # The previously unassigned ticket is attributed to the releasing operator.
            assert t_open.assignee_id == op.id
        finally:
            await _cleanup(db, [session], [op])

    from app.core.redis_client import close_redis

    await close_redis()


@pytest.mark.asyncio
async def test_release_with_resume_also_resolves_tickets():
    """结束接管（AI 恢复）同样收尾工单：会话回 active 后不在任何工作台列表里，
    工单若继续挂在坐席名下就成了只进角标、不进列表的幽灵计数。客户再求助时
    create_handoff 会重新建单（见 test_create_handoff_idempotent_and_reescalates）。"""
    if not await _db_up():
        pytest.skip("postgres not available")
    if not await _redis_up():
        pytest.skip("redis not available")

    from app.db.session import session_scope
    from app.services import takeover as tk

    async with session_scope() as db:
        op = _operator("K")
        session = _session(status=SessionStatus.HUMAN_TAKEOVER)
        db.add_all([op, session])
        await db.flush()
        ticket = _ticket(session, HandoffStatus.IN_PROGRESS)
        ticket.assignee_id = op.id
        db.add(ticket)
        await db.commit()

        try:
            resolved = await tk.end_takeover(db, session, resume_ai=True, operator=op)
            await db.commit()
            assert resolved == [str(ticket.id)]
            assert session.status == SessionStatus.ACTIVE
            await db.refresh(ticket)
            assert ticket.status == HandoffStatus.RESOLVED
            assert ticket.resolution_note == "坐席结束接管（AI 接续）"
            assert ticket.resolved_at is not None
        finally:
            await _cleanup(db, [session], [op])

    from app.core.redis_client import close_redis

    await close_redis()


@pytest.mark.asyncio
async def test_takeover_writes_and_clears_attribution_columns():
    """start_takeover 把归属写进 takeover_by_id/name 专列（不再写 session.meta），
    end_takeover 结束时清空这两列。"""
    if not await _db_up():
        pytest.skip("postgres not available")
    if not await _redis_up():
        pytest.skip("redis not available")

    from app.db.session import session_scope
    from app.services import takeover as tk

    async with session_scope() as db:
        op = _operator("T")
        session = _session(status=SessionStatus.ESCALATED)
        db.add_all([op, session])
        await db.commit()

        try:
            await tk.start_takeover(db, session, operator=op)
            await db.commit()
            await db.refresh(session)
            assert session.status == SessionStatus.HUMAN_TAKEOVER
            assert session.takeover_by_id == op.id
            assert session.takeover_by_name == "坐席T"
            # 归属写在专列上，绝不污染 session.meta。
            assert "takeover_by" not in (session.meta or {})

            await tk.end_takeover(db, session, resume_ai=True, operator=op)
            await db.commit()
            await db.refresh(session)
            assert session.takeover_by_id is None
            assert not session.takeover_by_name
        finally:
            await _cleanup(db, [session], [op])

    from app.core.redis_client import close_redis

    await close_redis()


@pytest.mark.asyncio
async def test_queue_counts_mine_dedupes_by_session():
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.conversations import queue_counts
    from app.db.session import session_scope

    async with session_scope() as db:
        me = _operator("M")
        s_ticket = _session()  # escalated, my in_progress ticket
        # 未认领工单、直接接管的会话：仅凭 takeover_by_id 就应计入「我的」。
        s_takeover = _session(status=SessionStatus.HUMAN_TAKEOVER)
        db.add_all([me, s_ticket, s_takeover])
        await db.flush()
        s_takeover.takeover_by_id = me.id
        s_takeover.takeover_by_name = me.name
        t1 = _ticket(s_ticket, HandoffStatus.IN_PROGRESS)
        t1.assignee_id = me.id
        # 接管会话上再挂一张我认领的工单：既在「工单归我」又在「接管归我」，不得重复计。
        t2 = _ticket(s_takeover, HandoffStatus.IN_PROGRESS)
        t2.assignee_id = me.id
        db.add_all([t1, t2])
        await db.commit()

        try:
            counts = await queue_counts(db=db, user=me)
            # s_ticket（我认领的工单）+ s_takeover（我接管但工单未认领）= 2，按会话去重。
            assert counts["mine"] == 2
            # takeover 由本用例 seed 的接管会话保证；waiting 口径（不含已认领）另见
            # test_waiting_excludes_claimed_sessions——此处两个 seed 会话都不算 waiting，
            # 故不对 waiting 断言，避免依赖共享库的残留数据。
            assert counts["takeover"] >= 1
            assert set(counts) == {"waiting", "mine", "takeover", "all"}
        finally:
            await _cleanup(db, [s_ticket, s_takeover], [me])


@pytest.mark.asyncio
async def test_waiting_excludes_claimed_sessions():
    """认领后会话应离开「待认领」计数：waiting 只数未被认领的 escalated 会话。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.conversations import queue_counts
    from app.db.session import session_scope

    async with session_scope() as db:
        me = _operator("W")
        s = _session()  # escalated, unclaimed
        db.add_all([me, s])
        await db.flush()  # 让 s.id 落定后再建工单（session_id 非空约束）
        t = _ticket(s, HandoffStatus.OPEN)
        db.add(t)
        await db.commit()
        try:
            base = await queue_counts(db=db, user=me)
            # 未认领：计入待认领。
            assert base["waiting"] >= 1
            # 认领后：该会话（仍 escalated）不再计入待认领，但计入「我的」。
            t.status = HandoffStatus.IN_PROGRESS
            t.assignee_id = me.id
            await db.commit()
            after = await queue_counts(db=db, user=me)
            assert after["waiting"] == base["waiting"] - 1
            assert after["mine"] == base["mine"] + 1
            # 「全部」是完整队列，不因认领而变化（会话仍 escalated）。
            assert after["all"] == base["all"]
        finally:
            await _cleanup(db, [s], [me])


@pytest.mark.asyncio
async def test_mine_count_ignores_tickets_on_departed_sessions():
    """幽灵计数回归：会话已离开人工队列（如 active/closed）时，滞留在它身上的
    in_progress 工单不得再计入 mine——正是「角标 2、列表空」bug 的口径修复。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.conversations import queue_counts
    from app.db.session import session_scope

    async with session_scope() as db:
        me = _operator("G")
        db.add(me)
        await db.flush()
        base = await queue_counts(db=db, user=me)

        s_active = _session(status=SessionStatus.ACTIVE)  # escalated 标志残留 True
        s_closed = _session(status=SessionStatus.CLOSED)
        db.add_all([s_active, s_closed])
        await db.flush()
        for s in (s_active, s_closed):
            t = _ticket(s, HandoffStatus.IN_PROGRESS)
            t.assignee_id = me.id
            db.add(t)
        await db.commit()

        try:
            after = await queue_counts(db=db, user=me)
            assert after["mine"] == base["mine"]  # 两张幽灵工单都不计入
            assert after["waiting"] == base["waiting"]
            assert after["all"] == base["all"]
        finally:
            await _cleanup(db, [s_active, s_closed], [me])


@pytest.mark.asyncio
async def test_close_session_resolves_tickets_and_clears_takeover():
    """客户结束会话必须收尾人工侧：存活工单全部 resolve、接管归属清空。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.services import takeover as tk

    async with session_scope() as db:
        op = _operator("C")
        session = _session(status=SessionStatus.HUMAN_TAKEOVER)
        db.add_all([op, session])
        await db.flush()
        session.takeover_by_id = op.id
        session.takeover_by_name = op.name
        ticket = _ticket(session, HandoffStatus.IN_PROGRESS)
        ticket.assignee_id = op.id
        db.add(ticket)
        await db.commit()

        try:
            resolved = await tk.close_session(db, session, rating=5, note="很满意")
            await db.commit()
            assert resolved == [str(ticket.id)]
            await db.refresh(session)
            assert session.status == SessionStatus.CLOSED
            assert session.satisfaction_rating == 5
            assert session.takeover_by_id is None
            assert not session.takeover_by_name
            await db.refresh(ticket)
            assert ticket.status == HandoffStatus.RESOLVED
            assert ticket.resolution_note == "客户结束会话"
        finally:
            await _cleanup(db, [session], [op])


@pytest.mark.asyncio
async def test_mark_handled_resolves_tickets():
    """「标记完成/已处理」让会话离队，必须同时 resolve 存活工单（含未认领的 open）。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.conversations import mark_handled
    from app.db.session import session_scope

    async with session_scope() as db:
        op = _operator("H")
        session = _session()  # escalated
        db.add_all([op, session])
        await db.flush()
        t_open = _ticket(session, HandoffStatus.OPEN)
        t_prog = _ticket(session, HandoffStatus.IN_PROGRESS)
        t_prog.assignee_id = op.id
        db.add_all([t_open, t_prog])
        await db.commit()

        try:
            out = await mark_handled(str(session.id), db=db, user=op)
            assert out["ok"] is True
            await db.refresh(session)
            assert session.status == SessionStatus.HUMAN_HANDLED
            for t in (t_open, t_prog):
                await db.refresh(t)
                assert t.status == HandoffStatus.RESOLVED
                assert t.resolution_note == "标记已处理"
            # 无人认领的 open 工单归到操作者名下，便于追溯。
            assert t_open.assignee_id == op.id
        finally:
            await _cleanup(db, [session], [op])


def _stub_notify(monkeypatch) -> list:
    """打桩 notify_operator：跑测试绝不能真发企微/邮件通知（共享 dev 库可能配了
    真 webhook）。返回调用记录列表供断言。"""
    calls: list = []

    async def _fake_notify(db, title, body_md):
        calls.append(title)
        return True, ""

    monkeypatch.setattr("app.services.handoff.notify_operator", _fake_notify)
    return calls


@pytest.mark.asyncio
async def test_create_handoff_idempotent_and_reescalates(monkeypatch):
    """create_handoff 契约：有存活工单幂等复用（不重复建单/降级状态）；
    会话回 active 后（escalated 标志残留 True）再转人工要能重新建单——
    否则客户收到「已为你转接」的假回执却永远进不了队列。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from sqlalchemy import func, select

    from app.db.session import session_scope
    from app.services.handoff import create_handoff

    notify_calls = _stub_notify(monkeypatch)

    async with session_scope() as db:
        # 场景1：AI 已恢复的会话（active + escalated=True，旧守卫在这里失效）重新建单。
        session = _session(status=SessionStatus.ACTIVE)
        db.add(session)
        await db.commit()

        try:
            t1 = await create_handoff(db, session, "user_request", "user_request", "再次求助")
            await db.commit()
            assert session.status == SessionStatus.ESCALATED
            assert t1.status == HandoffStatus.OPEN

            # 场景2：已有存活工单时幂等复用，不新建；原通知已送达则不重发。
            t2 = await create_handoff(db, session, "user_request", "user_request", "重复点击")
            await db.commit()
            assert t2.id == t1.id
            n = (
                await db.execute(
                    select(func.count(HandoffTicket.id)).where(
                        HandoffTicket.session_id == session.id
                    )
                )
            ).scalar_one()
            assert n == 1
            assert len(notify_calls) == 1  # 仅首次建单通知一次

            # 场景2b：原通知失败（notified=False）的，复用时补发一次。
            t1.notified = False
            await db.commit()
            await create_handoff(db, session, "user_request", "user_request", "再次点击")
            await db.commit()
            assert len(notify_calls) == 2
            await db.refresh(t1)
            assert t1.notified is True

            # 场景3：接管中不降级——复用工单且状态保持 human_takeover。
            session.status = SessionStatus.HUMAN_TAKEOVER
            await db.commit()
            t3 = await create_handoff(db, session, "user_request", "user_request", "接管中点转人工")
            await db.commit()
            assert t3.id == t1.id
            assert session.status == SessionStatus.HUMAN_TAKEOVER
        finally:
            await _cleanup(db, [session], [])


@pytest.mark.asyncio
async def test_resolve_last_ticket_returns_session_to_active():
    """后台解决工单的会话回迁：最后一张存活工单 resolve 后，escalated 会话回 active；
    还有存活工单时不动。否则会话滞留「待认领」却无单可认领。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.db.session import session_scope
    from app.services.handoff import resolve_handoff

    async with session_scope() as db:
        op = _operator("V")
        session = _session()  # escalated
        db.add_all([op, session])
        await db.flush()
        t1 = _ticket(session, HandoffStatus.OPEN)
        t2 = _ticket(session, HandoffStatus.OPEN)
        db.add_all([t1, t2])
        await db.commit()

        try:
            await resolve_handoff(db, t1, "线下已处理", op.id)
            await db.commit()
            await db.refresh(session)
            assert session.status == SessionStatus.ESCALATED  # 还有 t2 存活

            await resolve_handoff(db, t2, "线下已处理", op.id)
            await db.commit()
            await db.refresh(session)
            assert session.status == SessionStatus.ACTIVE
        finally:
            await _cleanup(db, [session], [op])


@pytest.mark.asyncio
async def test_ws_request_human_reescalates_after_service(monkeypatch):
    """WS 守卫回归（二次转人工静默丢失）：接待过一次的会话（escalated 标志残留
    True、状态已回 active）再点「转人工」必须重新建单入队——旧守卫
    `not s.escalated` 会静默丢弃该请求，客户却收到「已为你转接」的假回执。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from sqlalchemy import select

    from app.api.chat.ws import _handle_request_human
    from app.db.session import session_scope

    _stub_notify(monkeypatch)

    async with session_scope() as db:
        session = _session(status=SessionStatus.ACTIVE)  # escalated=True 残留
        db.add(session)
        await db.commit()
        sid = session.id

    try:
        await _handle_request_human(str(sid))
        async with session_scope() as db:
            s = await db.get(Session, sid)
            assert s.status == SessionStatus.ESCALATED
            tickets = (
                await db.execute(
                    select(HandoffTicket).where(HandoffTicket.session_id == sid)
                )
            ).scalars().all()
            assert len(tickets) == 1
            assert tickets[0].status == HandoffStatus.OPEN
    finally:
        async with session_scope() as db:
            s = await db.get(Session, sid)
            if s:
                await _cleanup(db, [s], [])


@pytest.mark.asyncio
async def test_ws_request_human_noop_during_takeover(monkeypatch):
    """接管中客户再点「转人工」：不建单、不把状态降级回 escalated（人工已在场）。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from sqlalchemy import func, select

    from app.api.chat.ws import _handle_request_human
    from app.db.session import session_scope

    _stub_notify(monkeypatch)

    async with session_scope() as db:
        session = _session(status=SessionStatus.HUMAN_TAKEOVER)
        db.add(session)
        await db.commit()
        sid = session.id

    try:
        await _handle_request_human(str(sid))
        async with session_scope() as db:
            s = await db.get(Session, sid)
            assert s.status == SessionStatus.HUMAN_TAKEOVER
            n = (
                await db.execute(
                    select(func.count(HandoffTicket.id)).where(
                        HandoffTicket.session_id == sid
                    )
                )
            ).scalar_one()
            assert n == 0
    finally:
        async with session_scope() as db:
            s = await db.get(Session, sid)
            if s:
                await _cleanup(db, [s], [])


@pytest.mark.asyncio
async def test_ws_end_session_runs_full_cleanup():
    """WS end_session 接线回归：必须走 close_session 统一收尾（置 CLOSED、落满意度、
    resolve 存活工单）——改回旧的三行内联赋值会让工单滞留。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.chat.ws import _handle_end_session
    from app.db.session import session_scope

    async with session_scope() as db:
        session = _session()  # escalated
        db.add(session)
        await db.flush()
        ticket = _ticket(session, HandoffStatus.OPEN)
        db.add(ticket)
        await db.commit()
        sid, tid = session.id, ticket.id

    try:
        await _handle_end_session(str(sid), {"rating": 4, "note": "服务不错"})
        async with session_scope() as db:
            s = await db.get(Session, sid)
            assert s.status == SessionStatus.CLOSED
            assert s.satisfaction_rating == 4
            assert s.satisfaction_note == "服务不错"
            t = await db.get(HandoffTicket, tid)
            assert t.status == HandoffStatus.RESOLVED
            assert t.resolution_note == "客户结束会话"
    finally:
        async with session_scope() as db:
            s = await db.get(Session, sid)
            if s:
                await _cleanup(db, [s], [])


@pytest.mark.asyncio
async def test_update_ticket_syncs_assignee_and_status():
    """update 接口认领/退回同步工单状态：不再产出「in_progress 且无人认领」
    （会话从待认领和所有人的我的同时消失、claim 又拒绝认领）的死态。"""
    if not await _db_up():
        pytest.skip("postgres not available")

    from app.api.admin.handoff import TicketUpdateIn, update_ticket
    from app.db.session import session_scope

    async with session_scope() as db:
        op = _operator("U")
        session = _session()
        db.add_all([op, session])
        await db.flush()
        ticket = _ticket(session, HandoffStatus.OPEN)
        db.add(ticket)
        await db.commit()

        try:
            await update_ticket(str(ticket.id), TicketUpdateIn(assignee="me"), db=db, user=op)
            await db.refresh(ticket)
            assert ticket.status == HandoffStatus.IN_PROGRESS
            assert ticket.assignee_id == op.id

            await update_ticket(str(ticket.id), TicketUpdateIn(assignee=""), db=db, user=op)
            await db.refresh(ticket)
            assert ticket.status == HandoffStatus.OPEN
            assert ticket.assignee_id is None
        finally:
            await _cleanup(db, [session], [op])
