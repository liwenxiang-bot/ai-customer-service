"""Resolve orphaned live handoff tickets + release stuck escalated sessions.

三条终结路径（客户结束会话 / 坐席结束接管(AI 恢复) / 标记完成）历史上都不收尾
工单，留下「会话已离开人工队列、工单仍 open/in_progress」的孤儿：已认领的那部分
把坐席工作台「我的」角标撑到比列表还多（幽灵计数），未认领的堆在后台工单队列。
反向地，后台对工单直接点「解决」不回迁会话状态，会话卡在 escalated 却无单可认领。
本迁移一次性清理两类存量脏数据；防再犯的代码修复见同 PR。

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 步骤1 孤儿工单：会话已不在人工队列（escalated/human_takeover），存活工单直接解决。
    op.execute(
        """
        UPDATE handoff_tickets t
        SET status = 'resolved',
            resolution_note = '历史数据修复：会话已离开人工队列，孤儿工单自动解决',
            resolved_at = NOW()
        FROM sessions s
        WHERE s.id = t.session_id
          AND t.status IN ('open', 'in_progress')
          AND s.status NOT IN ('escalated', 'human_takeover')
        """
    )
    # 步骤2 同会话重复存活工单去重：旧版 create_handoff 无查重，模型 escalate 工具在
    # 已升级会话上反复触发会积累多张（如一张已认领 in_progress + 一张更新的 open），
    # 造成「waiting 角标少 1、列表多 1」且同一会话同时占据待认领和我的两个队列。
    # 保留优先级：认领中(in_progress) > 最新创建；其余置 resolved。
    op.execute(
        """
        WITH ranked AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY session_id
                ORDER BY (status = 'in_progress') DESC, created_at DESC, id DESC
            ) AS rn
            FROM handoff_tickets
            WHERE status IN ('open', 'in_progress')
        )
        UPDATE handoff_tickets t
        SET status = 'resolved',
            resolution_note = '历史数据修复：同会话重复存活工单，保留认领中/最新的一张',
            resolved_at = NOW()
        FROM ranked r
        WHERE r.id = t.id AND r.rn > 1
        """
    )
    # 步骤3 滞留会话：工单全部已解决却仍卡在 escalated 的，放回 active（AI 继续服务，
    # 客户可再转人工）。三步的行集互不相交、彼此可交换；合在一起把存量数据收敛到
    # 新不变量：「escalated ⇔ 恰有一张存活工单归属该会话」。
    op.execute(
        """
        UPDATE sessions s
        SET status = 'active'
        WHERE s.status = 'escalated'
          AND NOT EXISTS (
            SELECT 1 FROM handoff_tickets t
            WHERE t.session_id = s.id AND t.status IN ('open', 'in_progress')
          )
        """
    )


def downgrade() -> None:
    # 数据修复不可逆：原始状态未另行留存，且修复后的代码不再产生这两类脏数据。
    pass
