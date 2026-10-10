"""Injectable time source. Everything time-dependent (quotas, cooldowns, schedules, cache TTLs)
reads the clock through this protocol so tests and Temporal replays are deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Manually advanced clock for tests and simulations."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_kst(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(KST)
