"""Historical telemetry queries with automatic aggregate routing.

A chart never reads raw samples for a 30-day window. The routing table below is
implemented once, here, and the chosen source is returned to the caller so the
UI can label the chart honestly instead of implying raw fidelity it does not
have.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MAX_POINTS = 10_000

# Two different limits, deliberately not one constant doing both jobs.
#
# PER_SERIES drives the bucket choice: how many points one line on a chart
# should have. ROWS is the hard cap on what a single request may return across
# every series, which protects the database and the browser and has to scale
# with the number of series asked for - a seven-metric chart legitimately wants
# seven times the rows of a one-metric chart.
#: What a LINE can carry, not what a response can. These are different budgets
#: and conflating them is what made a week denser than a day: 2500 points into a
#: plot ~650 units wide is four per pixel, so adjacent samples of a noisy signal
#: overprint into a band and the trajectory disappears inside it.
#:
#: ~200 leaves roughly three units between points, and - the part that actually
#: matters - it forces a bucket wide enough that the averaging does the
#: smoothing. Measured on one CPU series: at 1m buckets the line spans 1-90% and
#: reads as noise; the same hours at 1h buckets span 36-60% and read as a trend.
#: The aggregation IS the trend extraction.
TARGET_POINTS_PER_SERIES = 200

#: A safety cap on the response, unrelated to legibility: it stops a
#: pathological request pulling the table into memory.
MAX_ROWS = 10_000

# (bucket duration, table, bucket column, label), finest first. The raw table
# is not in here: it is reachable only by asking for interval=raw, because its
# density depends on the poll interval and is not something to route to by
# accident.
_BUCKETS = [
    (timedelta(minutes=1), "telemetry_1m", "bucket", "1m"),
    (timedelta(minutes=5), "telemetry_5m", "bucket", "5m"),
    (timedelta(hours=1), "telemetry_1h", "bucket", "1h"),
    (timedelta(days=1), "telemetry_1d", "bucket", "1d"),
]

_AGG_COLUMN = {"avg": "avg_value", "min": "min_value",
               "max": "max_value", "last": "last_value"}


def choose_source(start: datetime, end: datetime,
                  interval: str = "auto") -> tuple[str, str, str]:
    """Return (table, bucket_column, interval_label).

    Chosen by how many points the window would produce, not by how long the
    window is. Routing on window length alone gives wildly different answers
    for the same cost: a 7-day window at 1-minute buckets is 10,080 points per
    series, which is both slow to ship and unreadable once drawn, while a
    30-day window at 5 minutes is 8,640. Targeting a point budget instead
    lands on 1h for a day and a week, and daily buckets for a month, which is
    what a chart actually wants.
    """
    if interval == "raw":
        return "telemetry_sample", "ts", "raw"
    if interval in ("1m", "5m", "1h", "1d"):
        return f"telemetry_{interval}", "bucket", interval

    window = max(end - start, timedelta(seconds=1))
    for bucket, table, col, label in _BUCKETS:
        if window / bucket <= TARGET_POINTS_PER_SERIES:
            return table, col, label
    # Nothing coarser exists. Daily over a multi-year window is still the right
    # answer; the caller gets more points than the budget rather than an error.
    return "telemetry_1d", "bucket", "1d"


async def history(
    session: AsyncSession,
    *,
    device_id: str,
    metrics: list[str],
    start: datetime,
    end: datetime,
    interval: str = "auto",
    agg: str = "avg",
    instance: str | None = None,
) -> tuple[list[dict[str, Any]], str, str, bool]:
    table, bucket_col, label = choose_source(start, end, interval)
    value_expr = "value" if table == "telemetry_sample" else _AGG_COLUMN.get(agg, "avg_value")

    # Scale the cap with the number of series requested, then bound it, so a
    # normal multi-metric chart is never truncated but a pathological request
    # still cannot pull the table into memory.
    # Generous against the routing target: the cap is a backstop, and a chart
    # that is a little over budget should be drawn, not truncated.
    row_cap = min(MAX_ROWS, 4 * TARGET_POINTS_PER_SERIES * max(1, len(metrics)))
    params: dict[str, Any] = {"device_id": device_id, "metrics": metrics,
                              "start": start, "end": end, "limit": row_cap + 1}
    instance_clause = ""
    if instance is not None:
        instance_clause = " AND t.instance = :instance"
        params["instance"] = instance

    rows = (await session.execute(text(f"""
        SELECT m.key AS metric, m.unit, t.instance,
               extract(epoch FROM t.{bucket_col}) * 1000 AS ts_ms,
               t.{value_expr} AS value
        FROM {table} t
        JOIN metric m ON m.id = t.metric_id
        WHERE t.device_id = CAST(:device_id AS uuid)
          AND m.key = ANY(:metrics)
          AND t.{bucket_col} >= :start AND t.{bucket_col} < :end
          {instance_clause}
        -- Newest first for the cap, so a request that does not fit keeps the
        -- END of the window. A chart missing "now" is useless; one missing its
        -- oldest hour is merely shorter.
        ORDER BY t.{bucket_col} DESC
        LIMIT :limit
    """), params)).mappings().all()

    truncated = len(rows) > row_cap
    if truncated:
        rows = rows[:row_cap]
    rows = list(reversed(rows))     # back into chart order

    series: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["metric"], r["instance"])
        s = series.setdefault(key, {"metric": r["metric"], "instance": r["instance"],
                                    "unit": r["unit"], "points": []})
        s["points"].append([float(r["ts_ms"]), float(r["value"])])

    return list(series.values()), table, label, truncated


async def latest_values(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    """Every metric the device currently reports, from device_state's hot set
    plus the newest raw sample for the rest."""
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (m.key, t.instance)
               m.key AS metric, m.unit, m.display_name, t.instance,
               t.value, t.ts, t.quality
        FROM telemetry_sample t
        JOIN metric m ON m.id = t.metric_id
        WHERE t.device_id = CAST(:id AS uuid) AND t.ts > now() - interval '1 hour'
        ORDER BY m.key, t.instance, t.ts DESC
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def top_by_metric(session: AsyncSession, *, metric: str, limit: int = 10,
                        room_id: str | None = None) -> list[dict[str, Any]]:
    where = "AND rm.id = CAST(:room AS uuid)" if room_id else ""
    rows = (await session.execute(text(f"""
        SELECT d.id::text, d.name, d.device_type,
               (ds.metrics -> :metric ->> 'v')::float AS value
        FROM device_state ds
        JOIN device d      ON d.id = ds.device_id
        LEFT JOIN rack r   ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm  ON rm.id = COALESCE(rr.room_id, d.room_id)
        WHERE ds.metrics ? :metric {where}
        ORDER BY value DESC NULLS LAST
        LIMIT :limit
    """), {"metric": metric, "limit": limit,
           **({"room": room_id} if room_id else {})})).mappings().all()
    return [dict(r) for r in rows]
