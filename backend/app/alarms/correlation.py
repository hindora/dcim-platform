"""Dependency suppression: one root cause, N symptoms.

When an OOB switch dies, every device whose management interface lands on it
stops answering. The devices are fine - only the path used to watch them is
gone. Presenting that as 60 equal alarms buries the one that matters, so the
symptoms are marked and folded under the root.

Suppression is a display and notification decision, never a data decision. A
symptom keeps its row, its severity and its history; it gains ``is_symptom``
and a pointer to the alarm that explains it, and it is released the moment the
root clears.

The dangerous half of this is knowing when NOT to suppress. Losing an A feed
while the B feed is healthy leaves the load running, so a downstream alarm at
that moment is NOT explained by the feed failure - it is a real, separate
fault, and hiding it under the power alarm is how a single-feed condition goes
unnoticed until the other side fails too. That check is why redundancy_side
exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("alarms.correlation")

# Only alarms that mean "I cannot see it" are suppressible. A high temperature
# or a failed PSU on a device behind a dead switch is still a real condition
# about that device and must never be folded away.
SUPPRESSIBLE_TYPES = frozenset({"endpoint_unreachable", "telemetry_stale"})

# What counts as a root on an upstream device. Today the fleet's only
# infrastructure-failure alarm is endpoint_unreachable; a dedicated pdu_tripped
# or ups_on_battery rule would slot in here for the power layer without any
# other change.
ROOT_TYPES = ("endpoint_unreachable",)

# Which end of a connection is UPSTREAM, per layer. This is NOT uniform, and
# assuming it is produces a correlation engine that silently explains nothing:
#
#   management: device -> OOB switch      (a=device,  b=switch)
#   fieldbus:   gateway -> field device   (a=gateway, b=device)
#   power:      feeder  -> load           (a=feeder,  b=load)
#
# Management runs the opposite way to the other two because the managed device
# is the one holding the cable end, while a gateway or a PDU owns the trunk.
_UPSTREAM_COL = {
    "management": ("b_device_id", "a_device_id"),
    "fieldbus": ("a_device_id", "b_device_id"),
    "power": ("a_device_id", "b_device_id"),
}

# Cheapest and most common explanation first. Power is last because it is the
# only one that can be vetoed by redundancy.
LAYER_ORDER = ("management", "fieldbus", "power")

# Two hops covers device -> access switch -> distribution switch, and
# load -> rack PDU -> RPP. Past that the "cause" is too far away to assert.
MAX_HOPS = 2


def has_surviving_feed(side_status: dict[str, bool]) -> bool:
    """True when at least one distribution path into the load is still healthy.

    ``side_status`` maps a redundancy side ('A', 'B', or '?' for a feed whose
    path could not be determined) to whether every feeder on that side is
    alarming.

    A load with a healthy side is still powered, so nothing downstream of it is
    explained by the failure - which is exactly the case the exit criterion for
    this phase turns on.
    """
    return any(not compromised for compromised in side_status.values())


async def _upstream_root(session: AsyncSession, device_id: str,
                         layer: str) -> dict[str, Any] | None:
    """The nearest active root alarm upstream of this device on one layer."""
    up_col, down_col = _UPSTREAM_COL[layer]
    # up_col/down_col come from a fixed dict, never from a request.
    sql = f"""
        WITH RECURSIVE up AS (
            SELECT CAST(:device AS uuid) AS dev, 0 AS hop
            UNION
            SELECT c.{up_col}, u.hop + 1
              FROM up u
              JOIN connection c ON c.{down_col} = u.dev
               AND c.layer = CAST(:layer AS layer_t)
               AND c.admin_state = 'enabled'
             WHERE u.hop < :max_hops
        )
        SELECT a.id::text        AS id,
               a.device_id::text AS device_id,
               a.alarm_type, a.severity::text AS severity,
               d.name            AS device_name,
               u.hop
          FROM up u
          JOIN alarm a  ON a.device_id = u.dev
          JOIN device d ON d.id = u.dev
         WHERE u.hop > 0
           AND a.state <> 'CLEARED'
           AND a.alarm_type = ANY(:root_types)
         -- Nearest first, then oldest: the upstream failure that started it.
         ORDER BY u.hop, a.first_seen
         LIMIT 1
    """
    row = (await session.execute(text(sql), {
        "device": device_id, "layer": layer, "max_hops": MAX_HOPS,
        "root_types": list(ROOT_TYPES),
    })).mappings().first()
    return dict(row) if row else None


async def feed_side_status(session: AsyncSession,
                           device_id: str) -> dict[str, bool]:
    """Per power path into this device, is every feeder on it alarming?

    Grouped by redundancy_side, so a dual-fed load yields two entries and a
    single-corded one yields a single entry. Feeds whose side could not be
    derived are grouped under '?' rather than silently merged with A: an
    unknown path is not evidence of redundancy.
    """
    rows = (await session.execute(text("""
        SELECT COALESCE(c.redundancy_side, '?')      AS side,
               bool_or(root.id IS NOT NULL)          AS compromised
          FROM connection c
          LEFT JOIN alarm root
                 ON root.device_id = c.a_device_id
                AND root.state <> 'CLEARED'
                AND root.alarm_type = ANY(:root_types)
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.admin_state = 'enabled'
           AND c.b_device_id = CAST(:device AS uuid)
         GROUP BY 1
    """), {"device": device_id, "root_types": list(ROOT_TYPES)})).all()
    return {side: bool(compromised) for side, compromised in rows}


async def mark_symptom(session: AsyncSession, *, alarm_id: str,
                       root_alarm_id: str) -> None:
    await session.execute(text("""
        UPDATE alarm
           SET is_symptom = true, root_cause_alarm_id = CAST(:root AS uuid)
         WHERE id = CAST(:id AS uuid)
    """), {"id": alarm_id, "root": root_alarm_id})


async def correlate(session: AsyncSession, *, alarm_id: str, device_id: str,
                    alarm_type: str) -> dict[str, Any] | None:
    """Fold a new alarm under an upstream root, if one explains it.

    Returns the root alarm when suppressed, otherwise None.
    """
    if alarm_type not in SUPPRESSIBLE_TYPES:
        return None

    for layer in LAYER_ORDER:
        root = await _upstream_root(session, device_id, layer)
        if not root:
            continue

        if layer == "power":
            sides = await feed_side_status(session, device_id)
            if has_surviving_feed(sides):
                # Still fed from another path, so the feed failure does not
                # explain this. Leave it visible: it is a genuine fault AND
                # the load is now running without redundancy.
                log.info("power root not applied; load still fed",
                         alarm_id=alarm_id, device_id=device_id,
                         root=root["device_name"], sides=sides)
                continue

        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("alarm suppressed under root", alarm_id=alarm_id, layer=layer,
                 root_alarm=root["id"], root_device=root["device_name"],
                 hops=root["hop"])
        return {**root, "layer": layer}
    return None


async def release_symptoms(session: AsyncSession,
                           root_alarm_id: str) -> list[dict[str, Any]]:
    """Un-suppress everything a now-cleared root was explaining.

    Without this a symptom stays hidden after its cause is fixed, and an
    operator is left with a device that is still broken and an alarm list that
    says nothing is wrong.
    """
    rows = (await session.execute(text("""
        UPDATE alarm
           SET is_symptom = false, root_cause_alarm_id = NULL
         WHERE root_cause_alarm_id = CAST(:root AS uuid)
           AND state <> 'CLEARED'
        RETURNING id::text AS id, device_id::text AS device_id, alarm_type,
                  severity::text AS severity
    """), {"root": root_alarm_id})).mappings().all()
    out = [dict(r) for r in rows]
    if out:
        log.info("symptoms released", root_alarm=root_alarm_id, count=len(out))
    return out
