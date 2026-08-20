"""Capacity inputs: coincident load percentiles, space, cooling and ports."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Location join used by every scope filter. device.room_id carries
# floor-standing plant; the rack chain carries everything else.
_LOC = """
      LEFT JOIN rack r      ON r.id = d.rack_id
      LEFT JOIN rack_row rr ON rr.id = r.row_id
      LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
"""

_SCOPE_SQL = {
    "rack": "d.rack_id = CAST(:scope_id AS uuid)",
    "room": "rm.id = CAST(:scope_id AS uuid)",
    "datacenter": "rm.datacenter_id = CAST(:scope_id AS uuid)",
}


def scope_clause(scope: str) -> str:
    if scope not in _SCOPE_SQL:
        raise ValueError(f"unknown scope {scope!r}")
    return _SCOPE_SQL[scope]


async def metric_id(session: AsyncSession, key: str) -> int | None:
    return (await session.execute(
        text("SELECT id FROM metric WHERE key = :k"), {"k": key})).scalar()


async def devices_in_scope(session: AsyncSession, *, scope: str, scope_id: str,
                           device_types: list[str] | None = None) -> list[str]:
    """Device ids in a scope, resolved once.

    Cheap on inventory, and it turns the telemetry query into a lookup on
    device_id - which the hypertable is indexed for - instead of a scan joined
    through three location tables. That change alone took the power percentile
    from 78 seconds to under one.
    """
    rows = (await session.execute(text(f"""
        SELECT d.id::text
          FROM device d
          {_LOC}
         WHERE d.lifecycle <> 'decommissioned'
           AND (CAST(:types AS text[]) IS NULL
                OR d.device_type::text = ANY(CAST(:types AS text[])))
           AND {scope_clause(scope)}
    """), {"scope_id": scope_id, "types": device_types})).scalars().all()
    return list(rows)


async def coincident_power(session: AsyncSession, *, device_ids: list[str],
                           power_metric_id: int, hours: int,
                           percentile: int) -> dict[str, Any]:
    """p95 and peak of the TOTAL load across a set of devices.

    The sum is taken per one-minute bucket first, and the percentile over those
    sums. Taking each device's percentile and adding them assumes every device
    peaks in the same minute, which they do not - that overstates the
    coincident load and strands capacity that is never actually used.

    Read from the 1-minute continuous aggregate rather than raw samples. Its
    avg_value is already one value per device per minute, which is what this
    needs; summing raw samples counted a device several times in any minute it
    reported more than once, and inflated a hall's load from 113 kW to 415.
    """
    if not device_ids:
        return {}
    row = (await session.execute(text("""
        WITH per_bucket AS (
            -- metric_id resolved by the caller rather than joined here. The
            -- join to metric defeated the (device_id, bucket) index and turned
            -- a 1.5 s query into 58 s.
            SELECT t.bucket, sum(t.avg_value) AS total_w
              FROM telemetry_1m t
             WHERE t.device_id = ANY(CAST(:ids AS uuid[]))
               AND t.metric_id = :mid
               AND t.instance = ''
               AND t.bucket > now() - make_interval(hours => :hours)
             GROUP BY 1
        )
        SELECT percentile_cont(:pct) WITHIN GROUP (ORDER BY total_w) AS p95_w,
               max(total_w) AS peak_w,
               avg(total_w) AS mean_w,
               count(*)     AS buckets
          FROM per_bucket
    """), {"ids": device_ids, "mid": power_metric_id, "hours": hours,
           "pct": percentile / 100.0})).mappings().first()
    return dict(row) if row else {}


async def space(session: AsyncSession, *, scope: str,
                scope_id: str) -> dict[str, Any]:
    """Rack units used and available.

    Counts only devices that occupy rails. Zero-U gear - vertical PDUs, strapped
    probes - is in the rack but consumes no U, and counting it would report a
    rack as full while every rail slot is empty.
    """
    row = (await session.execute(text(f"""
        WITH in_scope AS (
            SELECT r.id, r.u_height
              FROM rack r
              LEFT JOIN rack_row rr ON rr.id = r.row_id
              LEFT JOIN room rm     ON rm.id = rr.room_id
             WHERE {'r.id = CAST(:scope_id AS uuid)' if scope == 'rack'
                    else 'rm.id = CAST(:scope_id AS uuid)' if scope == 'room'
                    else 'rm.datacenter_id = CAST(:scope_id AS uuid)'}
        )
        -- Racks first, THEN sum: sum(DISTINCT u_height) sums unique values, so
        -- eight identical 42 U racks came to 42 U and the room read as 279%
        -- full.
        SELECT (SELECT count(*) FROM in_scope)                 AS racks,
               (SELECT COALESCE(sum(u_height), 0) FROM in_scope) AS u_total,
               COALESCE(sum(d.u_height) FILTER (
                   WHERE d.u_start IS NOT NULL
                     AND d.lifecycle <> 'decommissioned'), 0)  AS u_used
          FROM in_scope
          LEFT JOIN device d ON d.rack_id = in_scope.id
    """), {"scope_id": scope_id})).mappings().first()
    return dict(row) if row else {}


async def cooling_capacity(session: AsyncSession, *, scope: str,
                           scope_id: str) -> dict[str, Any]:
    """Installed cooling, which on this fleet exists only at plant level.

    Not from the CRAH units: their Cooling_Capacity point is a DUTY PERCENTAGE,
    not kilowatts - the simulator declares the chiller's in kW and the CRAH's in
    percent, and the mapping reflects that. A CRAH running at 65% tells you
    nothing about how many kilowatts it can move.

    So the figure comes from the chillers, whose capacity point is in kW, and it
    is only offered for a datacenter scope. Splitting plant capacity across
    rooms would need to know how the chilled water is apportioned, which is not
    modelled, and a room-level number invented from a plant total is worse than
    no number.
    """
    if scope != "datacenter":
        return {"units": 0, "capacity_kw": None, "reason": "room_scope"}

    row = (await session.execute(text("""
        SELECT count(*) AS units, COALESCE(sum(rated_kw), 0) AS capacity_kw
          FROM (
              SELECT d.id, max(t.value) / 1000.0 AS rated_kw
                FROM device d
                JOIN telemetry_sample t ON t.device_id = d.id
                JOIN metric m ON m.id = t.metric_id
                LEFT JOIN rack r      ON r.id = d.rack_id
                LEFT JOIN rack_row rr ON rr.id = r.row_id
                LEFT JOIN room rm     ON rm.id = COALESCE(rr.room_id, d.room_id)
               WHERE d.device_type = 'chiller'
                 AND d.lifecycle <> 'decommissioned'
                 AND m.key = 'cooling_capacity'
                 AND t.ts > now() - interval '24 hours'
                 AND rm.datacenter_id = CAST(:scope_id AS uuid)
               GROUP BY d.id
              HAVING max(t.value) > 0
          ) rated
    """), {"scope_id": scope_id})).mappings().first()
    return dict(row) if row else {}


async def ports(session: AsyncSession, *, scope: str,
                scope_id: str) -> dict[str, Any]:
    """Switch ports, and how many are carrying a link.

    "Used" is inferred from operational state rather than from patch records,
    because the connection graph only carries interface terminations for a
    fraction of its links. A port that is operationally up has something on the
    other end; a port that is down may be patched and idle, so this reads as a
    lower bound on usage and is labelled inferred.
    """
    row = (await session.execute(text(f"""
        SELECT count(*)                                   AS total_ports,
               count(*) FILTER (WHERE up.value)           AS used_ports,
               count(DISTINCT d.id)                       AS switches
          FROM device d
          {_LOC}
          JOIN interface i ON i.device_id = d.id AND i.role = 'data'
          LEFT JOIN LATERAL (
              SELECT tb.value
                FROM telemetry_bool tb
                JOIN metric m ON m.id = tb.metric_id
               WHERE tb.device_id = d.id
                 AND m.key = 'if_oper_state'
                 AND tb.instance = i.name
                 AND tb.ts > now() - interval '30 minutes'
               ORDER BY tb.ts DESC
               LIMIT 1
          ) up ON TRUE
         WHERE d.device_type IN ('switch', 'oob_switch', 'router')
           AND d.lifecycle <> 'decommissioned'
           AND {scope_clause(scope)}
    """), {"scope_id": scope_id})).mappings().first()
    return dict(row) if row else {}


async def scope_name(session: AsyncSession, *, scope: str,
                     scope_id: str) -> str | None:
    table = {"rack": "rack", "room": "room", "datacenter": "datacenter"}[scope]
    col = "code" if scope == "datacenter" else "name"
    return (await session.execute(text(
        f"SELECT {col} FROM {table} WHERE id = CAST(:id AS uuid)"),
        {"id": scope_id})).scalar()
