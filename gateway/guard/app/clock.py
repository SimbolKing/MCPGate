from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def now() -> datetime:
    return utc_now()


def get_clock(request: Request) -> Clock:
    clock: Clock | None = getattr(request.app.state, "clock", None)
    return clock if clock is not None else utc_now


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_utc_naive(dt: datetime) -> datetime:
    return ensure_utc(dt).replace(tzinfo=None)


def to_timezone(dt: datetime, tz_name: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name))


def format_iso(dt: datetime | None, tz_name: str) -> str | None:
    if dt is None:
        return None
    return to_timezone(dt, tz_name).isoformat(timespec="seconds")


def parse_iso(value: str, default_tz: str = "Asia/Shanghai") -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return ensure_utc(dt)
