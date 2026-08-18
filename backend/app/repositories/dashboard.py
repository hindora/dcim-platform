"""Dashboard aggregates.

One request, served from device_state and the summary joins. The dashboard must
never fan out into a dozen calls: it is the page everyone leaves open.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def device_counts(session: AsyncSession,
                        datacenter_id: str | None = None) -> dict[str, int]:
    where = ""
    params: dict[str, Any] = {}
    if datacenter_id:
        where = """
            AND COALESCE(rm.datacenter_id, rm2.datacenter_id) = CAST(:dc AS uuid)
        """
        params["dc"] = datacenter_id

    row = (await session.execute(text(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE ds.status = 'ONLINE')   AS online,
               count(*) FILTER (WHERE ds.status = 'OFFLINE')  AS offline,
               count(*) FILTER (WHERE ds.status = 'DEGRADED') AS degraded,
               count(*) FILTER (WHERE ds.status IS NULL
                                   OR ds.status = 'UNKNOWN')  AS unknown
        FROM device d
        LEFT JOIN device_state ds ON ds.device_id = d.id
        LEFT JOIN rack r      ON r.id = d.rack_id
        LEFT JOIN rack_row rr ON rr.id = r.row_id
        LEFT JOIN room rm     ON rm.id = rr.room_id
        LEFT JOIN room rm2    ON rm2.id = d.room_id
        WHERE d.lifecycle <> 'decommissioned' {where}
    """), params)).mappings().first()
    return dict(row) if row else {}


async def power_summary(session: AsyncSession) -> dict[str, Any]:
    """IT load is the sum over IT device types only.

    Summing every device's power_draw would double count: a PDU reports the
    draw of the servers plugged into it. Splitting by category is the only
    honest way to get an IT number out of a flat metric.
    """
    row = (await session.execute(text("""
        SELECT
          COALESCE(sum(ds.power_w) FILTER (WHERE dt.category IN ('it','network')), 0)/1000.0
              AS it_load_kw,
          COALESCE(sum(ds.power_w) FILTER (WHERE dt.category = 'cooling'), 0)/1000.0
              AS cooling_load_kw,
          count(*) FILTER (WHERE ds.power_w IS NOT NULL) AS reporting_devices
        FROM device_state ds
        JOIN device d       ON d.id = ds.device_id
        JOIN device_type dt ON dt.code = d.device_type
        WHERE d.lifecycle <> 'decommissioned'
    """))).mappings().first()
    return dict(row) if row else {}


async def environment_summary(session: AsyncSession) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT round(avg(ds.inlet_temp_c), 1)  AS avg_inlet_c,
               max(ds.inlet_temp_c)            AS max_inlet_c,
               round(avg(ds.humidity_pct), 1)  AS avg_humidity_pct,
               count(*) FILTER (WHERE ds.inlet_temp_c > 27) AS hot_spots
        FROM device_state ds
        JOIN device d ON d.id = ds.device_id
        WHERE d.lifecycle <> 'decommissioned' AND ds.inlet_temp_c IS NOT NULL
    """))).mappings().first()
    return dict(row) if row else {}


async def collectors(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id, version, hostname, endpoints_owned, endpoints_online,
               last_heartbeat, stats,
               CASE WHEN last_heartbeat < now() - interval '60 seconds' THEN 'STALE'
                    ELSE status END AS status
        FROM collector_instance ORDER BY id
    """))).mappings().all()
    return [dict(r) for r in rows]


async def ingest_health(session: AsyncSession) -> dict[str, Any]:
    """Freshness of the newest telemetry row.

    This is the single number that says whether the pipeline is alive. A backend
    that is up but forty minutes behind is not healthy, and nothing else in the
    system will tell you.
    """
    row = (await session.execute(text("""
        SELECT max(ts) AS newest_sample,
               extract(epoch FROM (now() - max(ts))) AS lag_seconds
        FROM telemetry_sample
        WHERE ts > now() - interval '1 hour'
    """))).mappings().first()
    return dict(row) if row else {}
