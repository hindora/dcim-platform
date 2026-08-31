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

from app.core.layers import UPSTREAM_COL
from app.core.logging import get_logger
from app.repositories.alarms import _SEV_RANK

log = get_logger("alarms.correlation")

# Only alarms that mean "I cannot see it" are suppressible. A high temperature
# or a failed PSU on a device behind a dead switch is still a real condition
# about that device and must never be folded away.
#
# telemetry_stale is deliberately NOT in here, though it looks like it belongs.
# It is raised only for endpoints whose poll is currently SUCCEEDING - that is
# the definition of reachable-but-silent - so an upstream "cannot see it" root
# cannot explain it. Suppressing it anyway produced exactly the wrong answer in
# testing: an endpoint polling happily 287 seconds ago was folded under an
# unreachable OOB switch, hiding the one condition the staleness sweep exists
# to surface.
SUPPRESSIBLE_TYPES = frozenset({"endpoint_unreachable"})

# What counts as a root on an upstream device. Today the fleet's only
# infrastructure-failure alarm is endpoint_unreachable; a dedicated pdu_tripped
# or ups_on_battery rule would slot in here for the power layer without any
# other change.
ROOT_TYPES = ("endpoint_unreachable",)

# Which end of a connection is upstream is NOT uniform across layers, and
# assuming it is produces a correlation engine that silently explains nothing -
# the traversal walks away from the cause instead of towards it. The map lives
# in app.core.layers because impact analysis needs the same fact.
_UPSTREAM_COL = UPSTREAM_COL

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


# ---------------------------------------------------------------- bands
#
# A warning rule and a critical rule on ONE measurement are two views of one
# condition, not two conditions. A CPU at 93 C crosses `cpu_temp_high` (>80)
# and `cpu_temp_critical` (>90) together, and the console showed both: two
# rows, two severities, two acknowledgements and two clears for one hot CPU.
#
# Measured on this fleet: three injected faults produced five alarms, and the
# WARNING for the temperature arrived a minute AFTER its CRITICAL because the
# two rules carry different dwells - so the list read as though the situation
# had improved while nothing had changed.
#
# ISA-18.2's position is one alarm per measurement point with a severity that
# escalates. This platform keeps the separate rules - they hold different
# thresholds, dwells and response classes, and both are genuinely true - and
# folds the lower under the higher, which is what the suppression machinery
# above already does for dependency roots. The record keeps both; the console
# shows the one that matters.

_BAND_ROOT = text("""
    WITH me AS (
        SELECT metric_key, operator, threshold
          FROM alarm_rule
         WHERE alarm_type = :alarm_type AND metric_key IS NOT NULL
         LIMIT 1
    ), higher AS (
        -- A band is HIGHER when its threshold is further along the direction
        -- the rule fires in. Severity is not the test: it is a label chosen by
        -- whoever wrote the rule, and two rules can share one.
        SELECT r.alarm_type, r.threshold
          FROM alarm_rule r, me
         WHERE r.enabled
           AND r.metric_key = me.metric_key
           AND r.alarm_type <> :alarm_type
           AND r.threshold IS NOT NULL AND me.threshold IS NOT NULL
           AND ((me.operator = '>' AND r.threshold > me.threshold)
             OR (me.operator = '<' AND r.threshold < me.threshold))
    )
    SELECT a.id::text AS id, a.alarm_type, a.severity::text AS severity
      FROM alarm a
      JOIN higher h ON h.alarm_type = a.alarm_type
     WHERE a.device_id = CAST(:device_id AS uuid)
       AND a.instance IS NOT DISTINCT FROM :instance
       AND a.state <> 'CLEARED'
       AND NOT a.is_symptom
     ORDER BY CASE (SELECT operator FROM me) WHEN '>' THEN -h.threshold
                                             ELSE h.threshold END
     LIMIT 1
""")

_LOWER_BANDS = text("""
    WITH me AS (
        SELECT metric_key, operator, threshold
          FROM alarm_rule
         WHERE alarm_type = :alarm_type AND metric_key IS NOT NULL
         LIMIT 1
    ), lower AS (
        SELECT r.alarm_type
          FROM alarm_rule r, me
         WHERE r.enabled
           AND r.metric_key = me.metric_key
           AND r.alarm_type <> :alarm_type
           AND r.threshold IS NOT NULL AND me.threshold IS NOT NULL
           AND ((me.operator = '>' AND r.threshold < me.threshold)
             OR (me.operator = '<' AND r.threshold > me.threshold))
    )
    SELECT a.id::text AS id, a.alarm_type, a.severity::text AS severity
      FROM alarm a
      JOIN lower l ON l.alarm_type = a.alarm_type
     WHERE a.device_id = CAST(:device_id AS uuid)
       AND a.instance IS NOT DISTINCT FROM :instance
       AND a.state <> 'CLEARED'
       AND a.id <> CAST(:alarm_id AS uuid)
""")


async def collapse_bands(session: AsyncSession, *, alarm_id: str,
                         device_id: str, alarm_type: str,
                         instance: str) -> dict[str, Any] | None:
    """Fold this alarm and its siblings into one visible band.

    Both directions, because either can happen first and neither order is
    unusual: a value that jumps straight past both thresholds raises the
    critical first, while a value that climbs raises the warning first. The
    dwells differ too, so the arrival order does not even follow the reading.

    Returns the higher-band alarm when THIS one was folded under it; otherwise
    folds any open lower bands under this one and returns None.
    """
    args = {"alarm_type": alarm_type, "device_id": device_id,
            "instance": instance or ""}

    root = (await session.execute(_BAND_ROOT, args)).mappings().first()
    if root:
        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("band folded under a higher one", alarm_id=alarm_id,
                 alarm_type=alarm_type, root_alarm=root["id"],
                 root_type=root["alarm_type"])
        return dict(root)

    lower = (await session.execute(
        _LOWER_BANDS, {**args, "alarm_id": alarm_id})).mappings().all()
    for row in lower:
        await mark_symptom(session, alarm_id=row["id"], root_alarm_id=alarm_id)
        log.info("lower band folded under this one", alarm_id=row["id"],
                 alarm_type=row["alarm_type"], root_alarm=alarm_id,
                 root_type=alarm_type)
    return None


#: The same condition, reported without saying which part.
#:
#: A trap says "this device is hot" and carries no instance; the rule watching
#: the same reading says "CPU Temp is hot" and carries the sensor's name. On an
#: instance-scoped metric those cannot share an alarm key - and must not, since
#: a dual-socket server's two CPU sensors are two faults - so they arrive as two
#: alarms for one fan.
#:
#: The qualified one is the root: it names the part, which is what somebody
#: acting on it needs. The unqualified one becomes its symptom, still there,
#: still linked, no longer a second line on the console.
_QUALIFIED_SIBLING = text("""
    SELECT id::text, alarm_type, instance, severity::text AS severity
      FROM alarm
     WHERE device_id = CAST(:device_id AS uuid)
       AND alarm_type = CAST(:alarm_type AS text)
       AND instance <> ''
       AND state <> 'CLEARED'
       AND NOT is_symptom
     -- Worst first: _SEV_RANK numbers CRITICAL 0, so ASC is most severe.
     -- Imported rather than rewritten, because a severity ranked one way here
     -- and another way in the alarm list is how a console starts disagreeing
     -- with itself.
     ORDER BY {rank} ASC, first_seen
     LIMIT 1
""".format(rank=_SEV_RANK.format(col="severity")))

#: The reverse: an unqualified alarm already open when a qualified one arrives.
_UNQUALIFIED_SIBLINGS = text("""
    SELECT id::text, alarm_type, severity::text AS severity
      FROM alarm
     WHERE device_id = CAST(:device_id AS uuid)
       AND alarm_type = CAST(:alarm_type AS text)
       AND instance = ''
       AND id <> CAST(:alarm_id AS uuid)
       AND state <> 'CLEARED'
       AND NOT is_symptom
""")


async def collapse_unqualified(session: AsyncSession, *, alarm_id: str,
                               device_id: str, alarm_type: str,
                               instance: str) -> dict[str, Any] | None:
    """Fold a part-less alarm under the one that names the part.

    Both directions, for the same reason bands need both: the trap usually
    arrives first, having been sent the moment the device noticed, but a poll
    that lands mid-climb can beat it.

    Only within one canonical alarm type. Two different conditions on one
    device are two conditions, however close together they appear.
    """
    if instance:
        # This alarm names a part. Anything unqualified of the same type is the
        # same condition, seen with less detail.
        rows = (await session.execute(_UNQUALIFIED_SIBLINGS, {
            "device_id": device_id, "alarm_type": alarm_type,
            "alarm_id": alarm_id})).mappings().all()
        for row in rows:
            await mark_symptom(session, alarm_id=row["id"],
                               root_alarm_id=alarm_id)
            log.info("unqualified alarm folded under a named instance",
                     alarm_id=row["id"], alarm_type=alarm_type,
                     root_alarm=alarm_id, instance=instance)
        return None

    root = (await session.execute(_QUALIFIED_SIBLING, {
        "device_id": device_id, "alarm_type": alarm_type})).mappings().first()
    if root:
        await mark_symptom(session, alarm_id=alarm_id, root_alarm_id=root["id"])
        log.info("unqualified alarm folded under a named instance",
                 alarm_id=alarm_id, alarm_type=alarm_type,
                 root_alarm=root["id"], instance=root["instance"])
        return dict(root)
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
