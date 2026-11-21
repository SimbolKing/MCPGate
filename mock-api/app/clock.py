from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Clock:
    return utc_now


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_timezone(dt: datetime, tz_name: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name))


def format_iso(dt: datetime | None, tz_name: str) -> str | None:
    if dt is None:
        return None
    return to_timezone(dt, tz_name).isoformat(timespec="seconds")


def days_since(dt: datetime, now: datetime, tz_name: str) -> int:
    start_local = to_timezone(dt, tz_name).date()
    end_local = to_timezone(now, tz_name).date()
    return (end_local - start_local).days
