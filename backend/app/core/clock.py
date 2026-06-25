"""Business-timezone helpers.

Storage stays UTC everywhere; only *day boundaries* for stats and daily rollups use the
business timezone (APP_TIMEZONE), so "今日" / 趋势 reflect the operator's local day rather
than the UTC day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def app_tz() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def app_today() -> date:
    """Today's date in the business timezone."""
    return datetime.now(app_tz()).date()


def app_day_start_utc(d: date) -> datetime:
    """The UTC instant of 00:00 on day `d` in the business timezone (for created_at filters)."""
    return datetime.combine(d, datetime.min.time(), tzinfo=app_tz()).astimezone(UTC)
