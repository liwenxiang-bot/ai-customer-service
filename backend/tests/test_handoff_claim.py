"""Integration: agent claim/unclaim contract + release auto-resolve + queue-counts mine.

契约 A（坐席认领）：
- claim 仅 open 且未分配时成功；同人重复认领幂等 200；已被他人认领 409。
- unclaim 仅本人或 admin 可退回，status→open、assignee 清空。
- release(resume_ai=False) 自动 resolve 该会话 open/in_progress 的工单。
- queue-counts 的 mine = 我认领的 in_progress 工单 + 我接管中的会话（按会话去重）。

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
async def test_release_with_resume_keeps_tickets_open():
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
        db.add(ticket)
        await db.commit()

        try:
            resolved = await tk.end_takeover(db, session, resume_ai=True, operator=op)
            await db.commit()
            assert resolved == []
            assert session.status == SessionStatus.ACTIVE
            await db.refresh(ticket)
            assert ticket.status == HandoffStatus.IN_PROGRESS
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
