"""Trap-and-poll reconciliation: a trap is advisory, the measurement decides.

A trap gets you the fast edge and nothing else. It is one UDP datagram with no
retry, no sequence and no acknowledgement, so the clear that ends a condition
is exactly as losable as the raise that started it - and losing the clear is
the one that hurts: the alarm stands forever with nothing able to resolve it.

Measured here, on one server: the simulator fired CPUNormal at 14:14:12 while
the collector was down for 33 seconds during a restart. The datagram hit a
closed port, the sending rule engine had already flipped out of alert and never
sent another, and three alarms sat open on a machine whose CPU the platform
could see was 39.9%.

That is not a simulator artefact. Real notification paths drop packets, real
receivers restart, and every NMS that treats traps as authoritative accumulates
alarms nobody can clear. The standard answer is this one: the trap raises, the
POLL confirms, and the measurement is what ends it.

Two paths, because two situations:

* the measurement DISAGREES - there is a rule for this metric on this kind of
  device, telemetry has been in its clear band for the rule's own clear dwell,
  and the condition is demonstrably over.

* nobody has said it again - there is no rule that covers the metric, which is
  the ordinary case for a server's CPU here, so nothing can positively contest
  the alarm. It ages out instead: not re-asserted for a long time AND the
  device is still delivering telemetry.

The second half of that AND is the whole safety of it. "We stopped hearing
about the condition" and "we stopped hearing anything at all" look identical
from the alarm table, and only the first means the condition ended. A device
that has genuinely gone dark keeps its alarms, which is the behaviour anyone
would want at 3am.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("alarms.reconcile")

#: Sources whose alarms a measurement may overrule.
#:
#: `threshold` is deliberately absent: those alarms are already governed by the
#: rule engine, which clears them on its own evidence with its own hysteresis.
#: Reconciling them here would be a second opinion on a question that already
#: has a first one, and the two would race.
RECONCILABLE_SOURCES = ("snmp_trap", "state")

#: How far below its own threshold a reading must fall to count as recovered,
#: when the trap declared a threshold but no rule offers a clear point.
#:
#: A device says "I crossed 90". Clearing the moment it reads 89.9 would flap
#: the alarm open and shut on a CPU hovering at the line, which is what a
#: rule's clear threshold exists to prevent - so where no rule states one, 5%
#: of the threshold stands in for it. It is a guess, and a rule's own number is
#: always preferred when there is one.
CLEAR_MARGIN = 0.05

#: How long a trap alarm may go un-re-asserted before it is aged out, when no
#: rule can contest it.
#:
#: Gear that re-sends while a condition holds does so on the order of minutes -
#: this plane re-raises every five. Thirty minutes is six of those, long enough
#: that a device with anything to say has said it, short enough that a lost
#: clear does not outlive the shift it happened on.
REASSERT_GRACE_S = 1800

#: How fresh telemetry must be for silence to count as evidence of recovery.
#:
#: The point is to distinguish "the condition ended" from "we cannot see this
#: device any more". Ten minutes is longer than every poll profile in the
#: fleet except the 600 s network one, which is exactly at it.
SEEING_IT_S = 660


# The measurement contradicts the alarm.
#
# The threshold comes from whichever source has the better claim to it:
#
#   a RULE covering this metric on this device type, whose clear_threshold an
#   operator chose, with hysteresis already thought about;
#
#   otherwise the alarm's own threshold - the number the DEVICE declared when
#   it raised the trap - with a margin standing in for the hysteresis nobody
#   configured.
#
# The second half is what makes this work at all for the case that started it:
# a server's CPU has no rule, deliberately, because a busy server is not a
# fault. Before this, that meant a CPU trap could only be resolved by another
# trap or by a timer. The trap said "93, limit 90" and the platform threw both
# numbers away.
#
# The LAST `need` samples, not every sample in the window: the readings that
# raised the alarm are in the window too, so taking the extreme over all of it
# would hold the alarm until they aged out.
_MEASURED_CLEAR = text("""
    WITH candidate AS (
        SELECT a.id, a.device_id, a.alarm_type, a.severity::text AS severity,
               d.name AS device_name, a.last_seen,
               coalesce(r.metric_key, a.metric_key)   AS metric_key,
               coalesce(r.operator, '>')              AS operator,
               coalesce(r.clear_threshold,
                        a.threshold * (1 - :margin))  AS clear_threshold,
               GREATEST(coalesce(r.clear_dwell_samples, 2), 2) AS need,
               (r.id IS NOT NULL)                     AS from_rule
          FROM alarm a
          JOIN device d ON d.id = a.device_id
          LEFT JOIN alarm_rule r ON r.alarm_type = a.alarm_type
                                AND r.enabled
                                AND r.metric_key IS NOT NULL
                                AND r.clear_threshold IS NOT NULL
                                AND r.operator IN ('>', '<')
                                AND (cardinality(r.device_types) = 0
                                  OR d.device_type = ANY(r.device_types))
         WHERE a.state <> 'CLEARED'
           AND a.source = ANY(:sources)
           -- Something has to name the measurement, and something has to name
           -- a limit. A bare link_down has neither and is left to the timer.
           AND coalesce(r.metric_key, a.metric_key) IS NOT NULL
           AND (r.clear_threshold IS NOT NULL OR a.threshold IS NOT NULL)
    ), ranked AS (
        SELECT c.id, c.device_id, c.device_name, c.alarm_type, c.severity,
               c.metric_key, c.operator, c.clear_threshold, c.need, c.from_rule,
               t.value,
               row_number() OVER (PARTITION BY c.id ORDER BY t.ts DESC) AS rn
          FROM candidate c
          JOIN metric m ON m.key = c.metric_key
          JOIN telemetry_sample t ON t.device_id = c.device_id
                                 AND t.metric_id = m.id
                                 -- Only readings taken AFTER the device last
                                 -- asserted the condition. A poll from before
                                 -- the fault is not evidence the fault ended -
                                 -- and it is the reading most likely to be
                                 -- sitting in the window, because polls run
                                 -- every minute or two while a trap arrives
                                 -- the instant the condition starts.
                                 --
                                 -- Live proof that this matters: an injected
                                 -- CPU fault raised at 08:20:42 was cleared
                                 -- five seconds later by a 63.2% reading taken
                                 -- at 08:17:50, three minutes before the CPU
                                 -- ever climbed.
                                 AND t.ts > c.last_seen
                                 AND t.ts > now() - make_interval(secs => :window_s)
    ), tail AS (
        SELECT id, device_id, device_name, alarm_type, severity, metric_key,
               operator, clear_threshold, need, from_rule,
               count(*)   AS samples,
               max(value) AS hi,
               min(value) AS lo
          FROM ranked
         WHERE rn <= need
         GROUP BY id, device_id, device_name, alarm_type, severity, metric_key,
                  operator, clear_threshold, need, from_rule
    )
    SELECT id::text AS id, device_id::text AS device_id, device_name,
           alarm_type, severity, metric_key, from_rule,
           CASE WHEN operator = '>' THEN hi ELSE lo END AS worst,
           clear_threshold
      FROM tail
     WHERE samples >= need
       AND ((operator = '>' AND hi < clear_threshold)
         OR (operator = '<' AND lo > clear_threshold))
""")


# Nothing has re-asserted it, and the device is still talking to us.
_AGED_OUT = text("""
    WITH candidate AS (
        SELECT a.id, a.device_id, a.alarm_type, a.severity::text AS severity,
               d.name AS device_name,
               extract(epoch FROM (now() - a.last_seen)) AS quiet_s
          FROM alarm a
          JOIN device d ON d.id = a.device_id
         WHERE a.state <> 'CLEARED'
           AND a.source = ANY(:sources)
           AND a.last_seen < now() - make_interval(secs => :grace_s)
           -- Only what nothing can MEASURE. An alarm that names a metric -
           -- from its rule or from the trap's own varbinds - is decided by
           -- the reading above, and ageing it out on a timer would pre-empt
           -- the better answer.
           --
           -- What is left is the genuinely unverifiable: a bare link_down, a
           -- state trap with no number attached. The timer is the last resort
           -- it was always meant to be, rather than the ordinary path.
           AND a.metric_key IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM alarm_rule r
                WHERE r.alarm_type = a.alarm_type
                  AND r.enabled
                  AND r.metric_key IS NOT NULL
                  AND r.clear_threshold IS NOT NULL
                  AND (cardinality(r.device_types) = 0
                    OR d.device_type = ANY(r.device_types))
           )
    )
    SELECT c.id::text AS id, c.device_id::text AS device_id, c.device_name,
           c.alarm_type, c.severity, round(c.quiet_s) AS quiet_s
      FROM candidate c
     WHERE EXISTS (
         -- The safety condition. Silence only means recovery if we can still
         -- hear the device at all; a machine that has gone dark keeps its
         -- alarms, because that is when they matter most.
         SELECT 1 FROM telemetry_sample t
          WHERE t.device_id = c.device_id
            AND t.ts > now() - make_interval(secs => :fresh_s)
     )
""")


async def measured_clear(session: AsyncSession, *, window_s: int = 1800,
                         margin: float = CLEAR_MARGIN) -> list[dict[str, Any]]:
    """Alarms whose own metric has been in the clear band long enough."""
    rows = (await session.execute(_MEASURED_CLEAR, {
        "sources": list(RECONCILABLE_SOURCES), "window_s": window_s,
        "margin": margin,
    })).mappings().all()
    return [dict(r) for r in rows]


async def aged_out(session: AsyncSession, *,
                   grace_s: int = REASSERT_GRACE_S,
                   fresh_s: int = SEEING_IT_S) -> list[dict[str, Any]]:
    """Alarms nothing has re-asserted, on devices we can still see."""
    rows = (await session.execute(_AGED_OUT, {
        "sources": list(RECONCILABLE_SOURCES),
        "grace_s": grace_s, "fresh_s": fresh_s,
    })).mappings().all()
    return [dict(r) for r in rows]


def measured_reason(row: dict[str, Any]) -> str:
    whose = "the rule's" if row.get("from_rule") else "the device's own"
    return (f"{row['metric_key']} is back past {whose} clear point "
            f"({round(float(row['worst']), 1)} against "
            f"{round(float(row['clear_threshold']), 1)}) and no clear arrived")


def aged_reason(row: dict[str, Any]) -> str:
    return (f"not re-asserted for {int(row['quiet_s']) // 60} min while the "
            f"device kept reporting; the clear was probably lost in transit")
