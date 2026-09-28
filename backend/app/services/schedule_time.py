"""When a discovery schedule next runs.

Two kinds. An INTERVAL schedule runs every N hours from whenever it last fired. A
TIMED one runs at a local time on chosen weekdays - "weekdays at 02:00" - in an
IANA timezone, because a quiet window is a local fact: 02:00 in Mumbai is not
02:00 in London, and neither is 02:00 UTC.

Pure functions, so the rules that decide when the management network gets swept
can be tested without a clock or a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: ISO weekday numbers: 1 = Monday ... 7 = Sunday.
ALL_DAYS = (1, 2, 3, 4, 5, 6, 7)


class ScheduleTimeError(ValueError):
    """A timing choice that cannot be honoured, in the operator's words."""


def parse_time(raw: Any) -> time:
    """"02:00" (or a time) as a time, on the minute."""
    if isinstance(raw, time):
        return raw.replace(second=0, microsecond=0)
    s = str(raw or "").strip()
    try:
        hh, mm = s.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        raise ScheduleTimeError(f"{s!r} is not a time of day, e.g. 02:00") from None


def parse_days(raw: Any) -> list[int]:
    days = sorted({int(d) for d in (raw or ALL_DAYS)})
    if not days or any(d not in ALL_DAYS for d in days):
        raise ScheduleTimeError("days are 1 (Monday) to 7 (Sunday), at least one")
    return days


def zone(name: Any) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleTimeError(f"{name!r} is not a timezone, e.g. Europe/London") from None


def longest_gap_hours(days: list[int]) -> int:
    """The longest wait between two runs - what "on time" is measured against.

    Daily is 24; weekdays is 72 (Friday to Monday); one day a week is 168.
    """
    ds = sorted(days)
    if len(ds) == 1:
        return 168
    gaps = [b - a for a, b in pairwise(ds)] + [ds[0] + 7 - ds[-1]]
    return max(gaps) * 24


def next_occurrence(run_at: time, days: list[int], tz: str, after: datetime) -> datetime:
    """The first run strictly after `after`, as an aware UTC datetime.

    Computed in the zone's own calendar, so a daily 02:00 stays 02:00 local
    across a DST change. A local time that does not exist that night (02:30 on
    the spring-forward date) resolves to its instant under the pre-change offset,
    which lands an hour later on the clock - the sweep still runs that night.
    """
    z = zone(tz)
    local = after.astimezone(z)
    for i in range(8):
        day = (local + timedelta(days=i)).date()
        if day.isoweekday() not in days:
            continue
        candidate = datetime.combine(day, run_at, tzinfo=z)
        if candidate > local:
            return candidate.astimezone(UTC)
    raise ScheduleTimeError("no run in the next week")  # unreachable with >= 1 day


def timing(run_at: Any, days: Any, tz: Any, interval_hours: Any,
           intervals: tuple[int, ...]) -> dict[str, Any]:
    """Validate a schedule's timing and return the columns to store."""
    if run_at not in (None, ""):
        t = parse_time(run_at)
        ds = parse_days(days)
        zone(tz)
        return {"run_at": t.strftime("%H:%M"), "days": ds, "timezone": str(tz).strip(),
                "interval_hours": longest_gap_hours(ds)}
    try:
        hours = int(interval_hours)
    except (TypeError, ValueError):
        hours = 0
    if hours not in intervals:
        raise ScheduleTimeError(
            f"interval must be one of {', '.join(map(str, intervals))} hours")
    return {"run_at": None, "days": None, "timezone": None, "interval_hours": hours}


def next_after(schedule: dict[str, Any], now: datetime) -> datetime:
    """When a schedule runs next, having just fired (or been re-timed) at `now`.

    From NOW, never from the old due time: after an outage a schedule runs once,
    late, rather than once for every slot it missed.
    """
    if schedule.get("run_at"):
        return next_occurrence(parse_time(schedule["run_at"]),
                               parse_days(schedule.get("days")),
                               schedule["timezone"], now)
    return now + timedelta(hours=int(schedule["interval_hours"]))
