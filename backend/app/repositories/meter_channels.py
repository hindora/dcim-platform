"""The commissioning schedule: which breaker each CT channel is clamped to."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Meters to import from: anything whose job is measuring something else. A
# device type rather than a hand-kept list of names, so a meter added by the
# fleet is imported without an edit here.
_METER_TYPES = ("energy_monitor",)


async def meters(session: AsyncSession) -> list[dict[str, Any]]:
    """Every meter worth asking, with the address and channel count to ask on.

    The channel count comes from the model name - an `EV2-84` has 84 CT
    channels - because asking a 42-channel meter about channel 60 is 18
    timeouts and the import is slow enough already.
    """
    rows = (await session.execute(text("""
        SELECT d.id::text AS id, d.name,
               host(COALESCE(e.address, d.mgmt_ip, d.primary_ip)) AS ip,
               COALESCE(m.name, '') AS model
          FROM device d
          LEFT JOIN model m ON m.id = d.model_id
          LEFT JOIN LATERAL (
               SELECT address FROM device_endpoint
                WHERE device_id = d.id AND protocol = 'bacnet' AND enabled
                ORDER BY (role = 'primary') DESC LIMIT 1
          ) e ON true
         WHERE d.device_type = ANY(:types)
           AND d.lifecycle <> 'decommissioned'
           AND COALESCE(e.address, d.mgmt_ip, d.primary_ip) IS NOT NULL
         ORDER BY d.name
    """), {"types": list(_METER_TYPES)})).mappings().all()
    return [dict(r) for r in rows]


async def resolve_names(session: AsyncSession,
                        names: list[str]) -> dict[str, str]:
    """Device name -> id, for the labels a meter reported.

    Exact match only. A label that nearly matches a device name is a label
    this platform does not understand, and guessing which rack an electrician
    meant is how a load ends up attributed to the wrong one.
    """
    if not names:
        return {}
    rows = (await session.execute(text("""
        SELECT name, id::text AS id FROM device
         WHERE name = ANY(:names) AND lifecycle <> 'decommissioned'
    """), {"names": names})).mappings().all()
    return {r["name"]: r["id"] for r in rows}


async def replace_for_meter(session: AsyncSession, meter_id: str,
                            channels: list[dict[str, Any]]) -> None:
    """Write one meter's schedule, replacing what was there.

    Replace rather than merge: a re-import is the answer to "the schedule
    changed", and a channel that has gone from the meter's own description has
    gone. Merging would leave a retired CT attributing load forever.

    Channels the meter did not answer for are not in *channels* at all - see
    `read_schedule` - so a dropped packet cannot retire a live branch here
    either. It leaves the previous row for that channel in place only if the
    whole meter answered nothing, which the caller checks.
    """
    await session.execute(
        text("DELETE FROM meter_channel WHERE meter_device_id = CAST(:m AS uuid)"),
        {"m": meter_id})
    if not channels:
        return
    await session.execute(text("""
        INSERT INTO meter_channel
               (meter_device_id, instance, branch_device_id, label, source)
        VALUES (CAST(:meter AS uuid), :instance,
                CAST(:branch AS uuid), :label, :source)
        ON CONFLICT (meter_device_id, instance) DO UPDATE
           SET branch_device_id = EXCLUDED.branch_device_id,
               label            = EXCLUDED.label,
               source           = EXCLUDED.source,
               discovered_at    = now()
    """), [{"meter": meter_id, "instance": c["instance"],
            "branch": c.get("branch_device_id"), "label": c.get("label"),
            "source": c.get("source", "bacnet")} for c in channels])


async def for_branches(session: AsyncSession,
                       device_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Branch device id -> the channel measuring it, and its latest reading.

    One row per branch: the newest `power_draw` sample on the meter, at that
    channel's instance, inside a window. Bounded because a meter that stopped
    reporting an hour ago must not present a stale reading as the live one -
    the same rule the rest of the platform reads last-known state under.

    A branch metered by two monitors (the two sides of a 2N pair) yields the
    channel with the newest sample; both are true, and the fresher one is the
    one worth showing.
    """
    if not device_ids:
        return {}
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (mc.branch_device_id)
               mc.branch_device_id::text AS branch_id,
               mc.instance,
               md.name                   AS meter_name,
               t.value                   AS power_w,
               t.ts
          FROM meter_channel mc
          JOIN device md ON md.id = mc.meter_device_id
          JOIN metric m  ON m.key = 'power_draw'
          JOIN telemetry_sample t
            ON t.device_id = mc.meter_device_id
           AND t.metric_id = m.id
           AND t.instance  = mc.instance
           AND t.ts > now() - interval '15 minutes'
         WHERE mc.branch_device_id = ANY(CAST(:ids AS uuid[]))
         ORDER BY mc.branch_device_id, t.ts DESC
    """), {"ids": device_ids})).mappings().all()
    return {r["branch_id"]: dict(r) for r in rows}


async def stats(session: AsyncSession) -> dict[str, int]:
    """How much of the estate has a schedule, for the import's own report."""
    row = (await session.execute(text("""
        SELECT count(*) AS channels,
               count(*) FILTER (WHERE branch_device_id IS NOT NULL) AS clamped,
               count(*) FILTER (WHERE branch_device_id IS NULL AND label IS NOT NULL)
                   AS unresolved,
               count(DISTINCT meter_device_id) AS meters
          FROM meter_channel
    """))).mappings().first()
    return {k: int(v or 0) for k, v in dict(row).items()}
