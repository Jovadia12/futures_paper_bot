"""Injectable clock so session, staleness, and rollover logic are testable."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_ny(self) -> datetime:
        return self.now().astimezone(NY)

    def ts(self) -> float:
        return self.now().timestamp()


class FakeClock(Clock):
    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("FakeClock requires an aware datetime")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(timezone.utc)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def trading_day(ts: datetime, roll_hour: int = 17) -> date:
    """CME-style trading day: activity at/after ``roll_hour`` NY belongs to the next day."""
    ny = ts.astimezone(NY)
    day = ny.date()
    if ny.hour >= roll_hour:
        day += timedelta(days=1)
    return day
