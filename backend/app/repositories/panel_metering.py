"""Which meter measures a device that does not measure itself.

A remote power panel is a cabinet of breakers. Unless it was ordered with a
branch-circuit monitoring card it has no network port and nothing to poll;
what an operator reads for it is the meter clamped onto its conductors - a
Verdigris EV2 bolted to the panel, a PowerLogic BCPM in it. The import wires
that as a power connection panel -> meter, the direction the current runs, so
the meter is found as a load of the panel whose type exists to measure.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Device types whose job is measuring something else. Kept beside the
# topology service's copy of the same idea (services/topology.py).
METER_TYPES = ("energy_monitor",)


async def meters_fed_from(session: AsyncSession,
                          device_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Device id -> the meters it feeds, each with its own state.

    `power_w` is the meter's headline reading - for a panel meter, the panel's
    supply - and `last_seen` says how fresh that is. A device feeding no meter
    is absent from the result.
    """
    if not device_ids:
        return {}
    rows = (await session.execute(text("""
        SELECT c.a_device_id::text                   AS device_id,
               m.id::text                            AS meter_id,
               m.name                                AS meter_name,
               COALESCE(ds.status::text, 'UNKNOWN')  AS status,
               ds.last_seen,
               ds.power_w
          FROM connection c
          JOIN device m ON m.id = c.b_device_id
                       AND m.device_type::text = ANY(:types)
                       AND m.lifecycle <> 'decommissioned'
          LEFT JOIN device_state ds ON ds.device_id = m.id
         WHERE c.layer = 'power'
           AND c.admin_state = 'enabled'
           AND c.a_device_id = ANY(CAST(:ids AS uuid[]))
         ORDER BY c.a_device_id, m.name
    """), {"ids": device_ids, "types": list(METER_TYPES)})).mappings().all()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["device_id"], []).append(dict(r))
    return out


async def circuit_schedule(session: AsyncSession, meter_id: str) -> dict[str, dict[str, Any]]:
    """CT channel -> the breaker load it was commissioned onto (meter_channel)."""
    rows = (await session.execute(text("""
        SELECT mc.instance, mc.label,
               mc.branch_device_id::text AS branch_id,
               b.name                    AS branch_name
          FROM meter_channel mc
          LEFT JOIN device b ON b.id = mc.branch_device_id
         WHERE mc.meter_device_id = CAST(:m AS uuid)
    """), {"m": meter_id})).mappings().all()
    return {r["instance"]: dict(r) for r in rows}
