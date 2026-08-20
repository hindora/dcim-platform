"""Device, endpoint and interface queries. All SQL for this domain lives here."""

from __future__ import annotations

import base64
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Devices are listed newest-first by name; the cursor is (name, id) so that
# concurrent inserts cannot make a page repeat or skip rows the way OFFSET does.
_LIST_BASE = """
    SELECT d.id::text, d.name, d.device_type,
           v.name AS vendor, m.name AS model,
           host(d.mgmt_ip)    AS mgmt_ip,
           host(d.primary_ip) AS primary_ip,
           COALESCE(ds.status::text, 'UNKNOWN')       AS status,
           COALESCE(ds.health::text, 'UNKNOWN')       AS health,
           COALESCE(ds.max_severity::text, 'CLEAR')   AS max_severity,
           ds.last_seen,
           dc.id::text AS datacenter_id, dc.code AS datacenter_code,
           rm.id::text AS room_id, rm.name AS room_name,
           rr.name AS row_name,
           r.id::text AS rack_id, r.name AS rack_name,
           d.u_start
    FROM device d
    LEFT JOIN vendor v        ON v.id = d.vendor_id
    LEFT JOIN model m         ON m.id = d.model_id
    LEFT JOIN device_state ds ON ds.device_id = d.id
    LEFT JOIN rack r          ON r.id = d.rack_id
    LEFT JOIN rack_row rr     ON rr.id = r.row_id
    LEFT JOIN room rm         ON rm.id = COALESCE(rr.room_id, d.room_id)
    LEFT JOIN datacenter dc   ON dc.id = rm.datacenter_id
"""


def encode_cursor(name: str, device_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([name, device_id]).encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, str] | None:
    try:
        name, device_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return name, device_id
    except Exception:
        return None


async def list_devices(
    session: AsyncSession,
    *,
    device_types: list[str] | None = None,
    status: list[str] | None = None,
    room_id: str | None = None,
    rack_id: str | None = None,
    datacenter_id: str | None = None,
    search: str | None = None,
    include_decommissioned: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    where = []
    params: dict[str, Any] = {"limit": limit + 1}

    if not include_decommissioned:
        where.append("d.lifecycle <> 'decommissioned'")
    if device_types:
        where.append("d.device_type = ANY(:device_types)")
        params["device_types"] = device_types
    if status:
        where.append("COALESCE(ds.status::text, 'UNKNOWN') = ANY(:status)")
        params["status"] = status
    if room_id:
        where.append("rm.id = CAST(:room_id AS uuid)")
        params["room_id"] = room_id
    if rack_id:
        where.append("d.rack_id = CAST(:rack_id AS uuid)")
        params["rack_id"] = rack_id
    if datacenter_id:
        where.append("dc.id = CAST(:datacenter_id AS uuid)")
        params["datacenter_id"] = datacenter_id
    if search:
        # Trigram index on name; the IP casts are cheap because the result set
        # is already narrowed by the other predicates in practice.
        where.append("(d.name ILIKE :search OR host(d.mgmt_ip) LIKE :like "
                     "OR host(d.primary_ip) LIKE :like OR d.serial_number ILIKE :search)")
        params["search"] = f"%{search}%"
        params["like"] = f"%{search}%"
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded:
            where.append("(d.name, d.id::text) > (:cur_name, :cur_id)")
            params["cur_name"], params["cur_id"] = decoded

    sql = _LIST_BASE
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d.name, d.id::text LIMIT :limit"

    rows = (await session.execute(text(sql), params)).mappings().all()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1]["name"], rows[-1]["id"])
    return [dict(r) for r in rows], next_cursor


async def count_devices(session: AsyncSession) -> int:
    return (await session.execute(text(
        "SELECT count(*) FROM device WHERE lifecycle <> 'decommissioned'"
    ))).scalar_one()


async def get_device(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    # Detail needs extra columns, and they must land in the SELECT list rather
    # than after it, so the shared base is patched rather than concatenated.
    sql = _LIST_BASE.replace(
        "           d.u_start\n",
        "           d.u_start, d.serial_number, d.asset_tag, d.u_height, d.facing,\n"
        "           d.lifecycle::text AS lifecycle, d.admin_state::text AS admin_state,\n"
        "           d.attributes\n",
    ) + " WHERE d.id = CAST(:id AS uuid)"
    row = (await session.execute(text(sql), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def get_device_state(session: AsyncSession, device_id: str) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT device_id::text, status::text AS status, health::text AS health,
               max_severity::text AS max_severity, active_alarms, last_seen, metrics
        FROM device_state WHERE device_id = CAST(:id AS uuid)
    """), {"id": device_id})).mappings().first()
    return dict(row) if row else None


async def list_endpoints(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT e.id::text, e.protocol::text AS protocol, e.role::text AS role,
               host(e.address) AS address, e.port, e.enabled,
               c.secret_hint AS credential_hint,
               p.interval_s AS poll_interval_s,
               COALESCE(es.status::text, 'UNKNOWN') AS status,
               es.last_seen, es.last_success, es.last_error, es.last_error_class,
               COALESCE(es.consecutive_failures, 0) AS consecutive_failures,
               es.last_latency_ms,
               -- Cumulative since the row was first written. The collector
               -- resets its own counters on restart and the writer keeps the
               -- stored value monotonic, so these are lifetime totals, not a
               -- recent window - the UI has to say so.
               COALESCE(es.poll_count, 0) AS poll_count,
               COALESCE(es.fail_count, 0) AS fail_count,
               COALESCE(es.timeout_count, 0) AS timeout_count,
               COALESCE(es.auth_fail_count, 0) AS auth_fail_count
        FROM device_endpoint e
        LEFT JOIN credential c     ON c.id = e.credential_id
        JOIN poll_profile p        ON p.id = e.poll_profile_id
        LEFT JOIN endpoint_state es ON es.endpoint_id = e.id
        WHERE e.device_id = CAST(:id AS uuid)
        ORDER BY e.protocol, e.role
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]


async def list_interfaces(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT id::text, if_index, name, role, speed_bps, host(ip) AS ip,
               admin_state::text AS admin_state
        FROM interface WHERE device_id = CAST(:id AS uuid)
        ORDER BY if_index NULLS LAST, name
    """), {"id": device_id})).mappings().all()
    return [dict(r) for r in rows]
