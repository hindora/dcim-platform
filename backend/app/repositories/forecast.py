"""Daily series for forecasting.

One value per day, because that is the granularity capacity planning works at.
Three decisions are baked into the SQL and each of them changes the answer.

**Today is excluded.** A day in progress has only the hours it has had, so its
daily peak is systematically lower than a complete day's. Include it and every
forecast acquires a downward kink at the right-hand edge - the trend reads as
if load were falling on the very day someone looks at it.

**Incomplete days are dropped, not patched.** A day the collector missed half of
is not a low day, it is an unknown day. Averaging over what did arrive would
report a quiet Tuesday that never happened. Dropped days leave a gap, and the
gap is counted and reported rather than interpolated over.

Completeness is measured in HOURS COVERED, not in samples received, and that
distinction is not academic: it threw away the only complete day this fleet
had. Power is polled every 120 s, so a flawless day yields about 720 one-minute
buckets, not 1440 - judged against 1440 it looks 50% collected and is dropped
as unknown. Counting distinct hours instead is independent of how often a
device is polled, and a real collector outage still shows up as missing hours.

**Days are UTC.** The daily peak of a site whose working day straddles midnight
UTC is split across two rows. It does not matter for a monthly trend, which is
what this feeds, but it would matter for a report on business hours, so it is
stated rather than assumed.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# A day needs data in this many of its 24 hours to count. Below it the day is
# dropped as unknown rather than reported as low. Hours, not samples: see the
# module docstring - the sample count scales with the poll interval, so any
# threshold expressed in samples silently retunes itself every time an
# operator changes a polling profile.
MIN_DAY_HOURS = 20

HOURS_PER_DAY = 24

_DAILY_POWER = text("""
    WITH per_min AS (
        -- Coincident load: sum ACROSS devices within each minute first. Taking
        -- each device's daily peak and adding them assumes every device peaks
        -- in the same minute, which overstates the load the room actually
        -- carries.
        SELECT t.bucket AS b, sum(t.avg_value) AS total_w
          FROM telemetry_1m t
         WHERE t.device_id = ANY(CAST(:ids AS uuid[]))
           AND t.metric_id = :mid
           AND t.instance = ''
           AND t.bucket >= date_trunc('day', now())
                           - make_interval(days => :days)
           -- Strictly before the start of today: a partial day would drag the
           -- trend down at exactly the point the eye is drawn to.
           AND t.bucket < date_trunc('day', now())
         GROUP BY 1
    )
    SELECT date_trunc('day', b) AS day,
           percentile_cont(:pct) WITHIN GROUP (ORDER BY total_w) AS p95_w,
           max(total_w)  AS peak_w,
           avg(total_w)  AS mean_w,
           count(*)      AS minutes,
           count(DISTINCT date_trunc('hour', b)) AS hours
      FROM per_min
     GROUP BY 1
     ORDER BY 1
""")


async def daily_power(session: AsyncSession, *, device_ids: list[str],
                      metric_id: int, days: int,
                      percentile: int = 95) -> dict[str, Any]:
    """Daily coincident load, oldest first, with gaps reported not filled."""
    if not device_ids or metric_id is None:
        return {"series": [], "dropped_days": 0, "gap_days": 0}

    rows = (await session.execute(_DAILY_POWER, {
        "ids": device_ids, "mid": metric_id, "pct": percentile / 100.0,
        "days": days,
    })).mappings().all()

    kept, dropped = [], 0
    for r in rows:
        if (r["hours"] or 0) < MIN_DAY_HOURS:
            dropped += 1
            continue
        kept.append({
            "day": r["day"],
            "p95_kw": float(r["p95_w"] or 0) / 1000.0,
            "peak_kw": float(r["peak_w"] or 0) / 1000.0,
            "mean_kw": float(r["mean_w"] or 0) / 1000.0,
            "minutes": int(r["minutes"] or 0),
            "hours": int(r["hours"] or 0),
        })

    # Calendar gaps between the days that survived. A forecast reads its input
    # as evenly spaced, so a missing Wednesday silently shifts every later day
    # one to the left unless it is known about.
    gaps = 0
    for a, b in pairwise(kept):
        gaps += max(0, (b["day"] - a["day"]).days - 1)

    return {"series": kept, "dropped_days": dropped, "gap_days": gaps}
