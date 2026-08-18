"""Collector assignment queries.

The DCIM database is the source of truth for what exists; the collector pulls
its work list from here rather than reading a static file, because the fleet
changes at runtime and a file goes stale within minutes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def assignment_endpoints(session: AsyncSession, collector_id: str,
                               protocols: list[str] | None = None) -> list[dict[str, Any]]:
    """Endpoints this collector owns.

    ``collector_id IS NULL`` means unsharded - any collector may take it. With
    more than one collector, set the column and the split becomes explicit.
    """
    where = ["e.enabled", "e.admin_state = 'enabled'",
             "d.lifecycle <> 'decommissioned'",
             "(e.collector_id IS NULL OR e.collector_id = :collector_id)"]
    params: dict[str, Any] = {"collector_id": collector_id}
    if protocols:
        where.append("e.protocol::text = ANY(:protocols)")
        params["protocols"] = protocols

    rows = (await session.execute(text(f"""
        SELECT e.id::text, e.device_id::text, d.name AS device_name, d.device_type,
               v.name AS vendor, m.name AS model,
               e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, e.port, e.addressing,
               e.via_endpoint_id::text,
               c.kind AS credential_kind, c.secret_enc,
               p.interval_s, p.timeout_ms, p.retries, p.metric_groups, p.push_enabled
        FROM device_endpoint e
        JOIN device d        ON d.id = e.device_id
        JOIN poll_profile p  ON p.id = e.poll_profile_id
        LEFT JOIN vendor v   ON v.id = d.vendor_id
        LEFT JOIN model m    ON m.id = d.model_id
        LEFT JOIN credential c ON c.id = e.credential_id
        WHERE {' AND '.join(where)}
        ORDER BY e.id
    """), params)).mappings().all()
    return [dict(r) for r in rows]


async def assignment_version(session: AsyncSession, collector_id: str) -> int:
    """A cheap version number for ETag purposes.

    Derived from the newest endpoint mutation rather than a counter table: any
    insert, update or soft delete moves ``updated_at``, so the ETag changes
    exactly when the assignment content changes.
    """
    row = (await session.execute(text("""
        SELECT count(*) AS n,
               COALESCE(extract(epoch FROM max(e.updated_at))::bigint, 0) AS newest
        FROM device_endpoint e
        JOIN device d ON d.id = e.device_id
        WHERE e.enabled AND d.lifecycle <> 'decommissioned'
          AND (e.collector_id IS NULL OR e.collector_id = :collector_id)
    """), {"collector_id": collector_id})).mappings().first()
    if not row:
        return 0
    # Combine count and newest mtime: a delete lowers the count even when no
    # timestamp advances.
    return int(row["newest"]) * 100_000 + int(row["n"])


async def upsert_heartbeat(session: AsyncSession, hb: dict[str, Any]) -> None:
    await session.execute(text("""
        INSERT INTO collector_instance (id, version, hostname, started_at,
                                        last_heartbeat, endpoints_owned,
                                        endpoints_online, status, stats)
        VALUES (:id, :version, :hostname, :started_at, now(),
                :endpoints_owned, :endpoints_online, 'HEALTHY', CAST(:stats AS jsonb))
        ON CONFLICT (id) DO UPDATE SET
            version = EXCLUDED.version,
            hostname = EXCLUDED.hostname,
            last_heartbeat = now(),
            endpoints_owned = EXCLUDED.endpoints_owned,
            endpoints_online = EXCLUDED.endpoints_online,
            status = 'HEALTHY',
            stats = EXCLUDED.stats
    """), hb)
