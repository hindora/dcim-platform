"""Discovery ranges: the address space somebody has said is worth auditing.

See migration 0082 for why a range is a record rather than a /24 derived from
inventory.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Seconds without a heartbeat before a collector reads as offline. Matches the
#: sharding default, so "offline" means the same thing on every page.
COLLECTOR_STALE_S = 60.0

_COLUMNS = """
    r.id::text, r.cidr::text AS cidr, r.name, r.datacenter_id::text AS datacenter_id,
    dc.name AS datacenter_name, r.purpose, r.collector_id,
    COALESCE(array(SELECT x::text FROM unnest(r.exclusions) x), '{}') AS exclusions,
    r.enabled, r.notes, r.created_by, r.created_at, r.updated_at
"""


async def list_ranges(session: AsyncSession) -> list[dict[str, Any]]:
    """Every range with what makes it readable at a glance.

    `known` is how many recorded devices answer somewhere inside it - on their
    management address or any polled endpoint - so "10.51.11.0/24, 105 known"
    says what a sweep finding 106 has found. `overlaps` names other ranges that
    share addresses: legitimate (a /22 with a /24 carved out for a row) but worth
    seeing, because a device in both is swept twice.
    """
    rows = (await session.execute(text(f"""
        SELECT {_COLUMNS},
               (SELECT count(DISTINCT d.id) FROM device d
                 WHERE d.lifecycle <> 'decommissioned'
                   AND (d.mgmt_ip <<= r.cidr
                        OR EXISTS (SELECT 1 FROM device_endpoint e
                                    WHERE e.device_id = d.id
                                      AND e.address <<= r.cidr))) AS known,
               ci.id IS NOT NULL AS collector_registered,
               extract(epoch FROM (clock_timestamp() - ci.last_heartbeat))
                   AS collector_age_s,
               COALESCE((SELECT array_agg(o.name ORDER BY o.cidr)
                           FROM discovery_range o
                          WHERE o.id <> r.id AND o.cidr && r.cidr), '{{}}') AS overlaps
          FROM discovery_range r
          LEFT JOIN datacenter dc ON dc.id = r.datacenter_id
          LEFT JOIN collector_instance ci ON ci.id = r.collector_id
         ORDER BY r.cidr
    """))).mappings().all()
    return [dict(r) for r in rows]


async def get_ranges(session: AsyncSession, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = (await session.execute(text(f"""
        SELECT {_COLUMNS}
          FROM discovery_range r
          LEFT JOIN datacenter dc ON dc.id = r.datacenter_id
         WHERE r.id = ANY(CAST(:ids AS uuid[]))
         ORDER BY r.cidr
    """), {"ids": ids})).mappings().all()
    return [dict(r) for r in rows]


async def get_range(session: AsyncSession, range_id: str) -> dict[str, Any] | None:
    got = await get_ranges(session, [range_id])
    return got[0] if got else None


async def cidr_taken(session: AsyncSession, cidr: str,
                     except_id: str | None = None) -> str | None:
    """The name of the range already holding exactly this CIDR, if any."""
    return (await session.execute(text("""
        SELECT name FROM discovery_range
         WHERE cidr = CAST(:c AS cidr)
           AND (CAST(:x AS uuid) IS NULL OR id <> CAST(:x AS uuid))
    """), {"c": cidr, "x": except_id})).scalar()


async def create_range(session: AsyncSession, values: dict[str, Any],
                       actor: str | None) -> dict[str, Any]:
    rid = (await session.execute(text("""
        INSERT INTO discovery_range (cidr, name, datacenter_id, purpose, collector_id,
                                     exclusions, enabled, notes, created_by)
        VALUES (CAST(:cidr AS cidr), :name, CAST(:datacenter_id AS uuid), :purpose,
                :collector_id, CAST(:exclusions AS cidr[]), :enabled, :notes, :actor)
        RETURNING id::text
    """), {**values, "actor": actor})).scalar_one()
    return await get_range(session, rid)  # type: ignore[return-value]


_UPDATABLE = {
    "cidr": "CAST(:cidr AS cidr)", "name": ":name",
    "datacenter_id": "CAST(:datacenter_id AS uuid)", "purpose": ":purpose",
    "collector_id": ":collector_id", "exclusions": "CAST(:exclusions AS cidr[])",
    "enabled": ":enabled", "notes": ":notes",
}


async def update_range(session: AsyncSession, range_id: str,
                       fields: dict[str, Any]) -> dict[str, Any] | None:
    sets = [f"{k} = {_UPDATABLE[k]}" for k in fields if k in _UPDATABLE]
    if sets:
        await session.execute(text(f"""
            UPDATE discovery_range SET {', '.join(sets)}, updated_at = now()
             WHERE id = CAST(:id AS uuid)
        """), {**fields, "id": range_id})
    return await get_range(session, range_id)


async def schedules_using(session: AsyncSession, range_id: str) -> list[str]:
    rows = (await session.execute(text("""
        SELECT name FROM discovery_schedule
         WHERE CAST(:id AS uuid) = ANY(range_ids) ORDER BY name
    """), {"id": range_id})).scalars().all()
    return list(rows)


async def delete_range(session: AsyncSession, range_id: str) -> bool:
    return bool((await session.execute(text("""
        DELETE FROM discovery_range WHERE id = CAST(:id AS uuid) RETURNING id
    """), {"id": range_id})).first())


async def suggestions(session: AsyncSession) -> list[dict[str, Any]]:
    """The /24s inventory's addresses fall in that NO saved range covers.

    A starting point, not the list: it can only ever show space something is
    already recorded in, so it never offers the unrecorded subnet an audit is
    for. Once a range covers a /24 it stops being suggested.
    """
    rows = (await session.execute(text("""
        WITH addr AS (
            SELECT d.mgmt_ip AS a FROM device d
             WHERE d.mgmt_ip IS NOT NULL AND d.lifecycle <> 'decommissioned'
               AND family(d.mgmt_ip) = 4
        )
        SELECT host(network(set_masklen(a, 24))) || '/24' AS cidr, count(*) AS known
          FROM addr
         WHERE NOT EXISTS (SELECT 1 FROM discovery_range r WHERE addr.a <<= r.cidr)
         GROUP BY 1
         ORDER BY count(*) DESC, 1
         LIMIT 64
    """))).mappings().all()
    return [dict(r) for r in rows]


async def collectors(session: AsyncSession) -> list[dict[str, Any]]:
    """Registered collectors and how recently each was heard, for the picker."""
    rows = (await session.execute(text("""
        SELECT id, hostname, last_heartbeat,
               extract(epoch FROM (clock_timestamp() - last_heartbeat)) AS age_s
          FROM collector_instance ORDER BY id
    """))).mappings().all()
    return [{**dict(r), "healthy": r["age_s"] is not None
             and float(r["age_s"]) < COLLECTOR_STALE_S} for r in rows]


async def datacenters(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(text(
        "SELECT id::text, name FROM datacenter ORDER BY name"))).mappings().all()
    return [dict(r) for r in rows]
