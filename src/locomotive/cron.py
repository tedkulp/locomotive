"""Cron expressions read in a local time zone, with the DST rules Schedules promise:

- a wall-clock time that doesn't exist (skipped by a spring-forward) doesn't fire;
- a wall-clock time that happens twice (repeated by a fall-back) fires once, at its first occurrence.

croniter walks naive wall-clock times (no DST at all); each one is then placed in the zone here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter

ALIASES = {"@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"}
# Wall-clock margin around a search start: covers a fall-back's repeated hour.
MARGIN = timedelta(hours=3)
# A cron that matches nothing (e.g. 30 February) gives up after this many candidates.
MAX_STEPS = 100_000


class InvalidCron(ValueError):
    pass


def validate(expr: str) -> str:
    """A standard five-field cron expression (or an @alias); no seconds field."""
    expr = " ".join(expr.split())
    if expr not in ALIASES and len(expr.split(" ")) != 5:
        raise InvalidCron(f"{expr!r} is not a five-field cron expression (minute hour day month weekday)")
    if not croniter.is_valid(expr):
        raise InvalidCron(f"{expr!r} is not a valid cron expression")
    return expr


def local_zone(name: str | None) -> ZoneInfo:
    """The named zone; unset means the host's (TZ, then /etc/localtime), falling back to UTC."""
    if name:
        return ZoneInfo(name)
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    link = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in link:
        candidates.append(link.split("zoneinfo/", 1)[1])
    for candidate in candidates:
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def _place(naive: datetime, zone: ZoneInfo) -> datetime | None:
    """The instant a wall-clock time names (its first occurrence), or None if it doesn't exist."""
    aware = naive.replace(tzinfo=zone, fold=0)
    if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        return None  # skipped by a spring-forward
    return aware.astimezone(UTC)


def next_after(expr: str, zone: ZoneInfo, after: datetime) -> datetime | None:
    """The first firing strictly after ``after`` (UTC), or None if the cron never matches."""
    it = croniter(expr, after.astimezone(zone).replace(tzinfo=None) - MARGIN)
    try:
        for _ in range(MAX_STEPS):
            when = _place(it.get_next(datetime), zone)
            if when is not None and when > after:
                return when
    except CroniterBadDateError:
        return None
    return None


def last_at_or_before(expr: str, zone: ZoneInfo, until: datetime) -> datetime | None:
    """The newest firing at or before ``until`` (UTC)."""
    it = croniter(expr, until.astimezone(zone).replace(tzinfo=None) + MARGIN)
    try:
        for _ in range(MAX_STEPS):
            when = _place(it.get_prev(datetime), zone)
            if when is not None and when <= until:
                return when
    except CroniterBadDateError:
        return None
    return None


def count_between(expr: str, zone: ZoneInfo, after: datetime, until: datetime, cap: int = 1000) -> int:
    """How many firings fall in (after, until], counting at most ``cap``."""
    count, when = 0, after
    while count < cap:
        nxt = next_after(expr, zone, when)
        if nxt is None or nxt > until:
            break
        count, when = count + 1, nxt
    return count
