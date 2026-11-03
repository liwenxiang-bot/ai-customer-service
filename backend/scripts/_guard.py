"""Shared safety guard for maintenance / evaluation scripts that WRITE to the database."""

from __future__ import annotations

import sys

from app.config import settings

CONFIRM_FLAG = "--i-understand-this-modifies-production"


def refuse_in_production(what: str) -> None:
    """Abort when APP_ENV=production unless the operator passes the explicit confirm flag."""
    if settings.is_production and CONFIRM_FLAG not in sys.argv:
        print(
            f"✋ 拒绝在生产环境运行：{what}\n"
            f"   该脚本会修改数据库。请在独立的评测库运行；确需在生产执行，追加参数 {CONFIRM_FLAG}",
            file=sys.stderr,
        )
        sys.exit(2)
