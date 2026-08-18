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

# (max window, table, bucket column, value expression by aggregate)
_ROUTES = [
    (timedelta(hours=6), "telemetry_sample", "ts", None),
    (timedelta(days=7), "telemetry_1m", "bucket", "1m"),
    (timedelta(days=90), "telemetry_5m", "bucket", "5m"),
    (None, "telemetry_1h", "bucket", "1h"),
]

_AGG_COLUMN = {"avg": "avg_value", "min": "min_value",
               "max": "max_value", "last": "last_value"}


def choose_source(start: datetime, end: datetime,
                  interval: str = "auto") -> tuple[str, str, str]:
    """Return (table, bucket_column, interval_label)."""
    if interval == "raw":
        return "telemetry_sample", "ts", "raw"
    if interval in ("1m", "5m", "1h"):
        return f"telemetry_{interval}", "bucket", interval

    window = end - start
    for limit, table, col, label in _ROUTES:
        if limit is None or window <= limit:
            return table, col, (label or "raw")
    return "telemetry_1h", "bucket", "1h"


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

    params: dict[str, Any] = {"device_id": device_id, "metrics": metrics,
                              "start": start, "end": end, "limit": MAX_POINTS + 1}
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
        ORDER BY t.{bucket_col}
        LIMIT :limit
    """), params)).mappings().all()

    truncated = len(rows) > MAX_POINTS
    if truncated:
        rows = rows[:MAX_POINTS]

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
