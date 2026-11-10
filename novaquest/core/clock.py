"""Time source. Services ask the clock for 'now' so tests can fix the time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    """Real wall clock (UTC)."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(timezone.utc)


class FixedClock(Clock):
    """A clock that only moves when told to. Used by tests."""

    def __init__(self, start: datetime) -> None:
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        """Return the fixed time."""
        return self._now

    def advance(self, **kwargs: float) -> None:
        """Move the clock forward, e.g. advance(hours=2)."""
        self._now += timedelta(**kwargs)

    def set(self, when: datetime) -> None:
        """Jump to an exact time."""
        self._now = when.astimezone(timezone.utc)


_clock: Clock = Clock()


def get_clock() -> Clock:
    """Return the active clock."""
    return _clock


def set_clock(clock: Clock) -> None:
    """Replace the active clock (tests only)."""
    global _clock
    _clock = clock


def now() -> datetime:
    """Shortcut for the active clock's current UTC time."""
    return _clock.now()


def to_ist(value: datetime) -> datetime:
    """Convert a UTC datetime to Indian Standard Time."""
    return value.astimezone(IST)


def parse_ist(local: str) -> datetime:
    """Parse an HTML datetime-local value entered in IST and return UTC."""
    naive = datetime.fromisoformat(local)
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


def humanize_left(until: datetime, ref: datetime | None = None) -> str:
    """Describe time remaining in plain words: '1 day 4 h', '35 min'."""
    delta = until - (ref or now())
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "closed"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days} day{'s' if days > 1 else ''} {hours} h"
    if hours:
        return f"{hours} h {mins} min"
    return f"{max(mins, 1)} min"
