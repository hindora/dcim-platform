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

Three paths, because three situations:

* the STATE contradicts the alarm, or upholds it. Some conditions are not a
  number crossing a line, they are a machine being off - and the platform
  polls that as a boolean beside the trap that announced it. A stopped CRAH
  keeps publishing "not running" every heartbeat, so there is a measurement
  here after all, and it is the one that decides.

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

And the timer must never outrank a measurement. Three CRAHs stopped by an
operator raised plant_unit_stopped, the units stayed stopped, and thirty
minutes later all three alarms aged out - while the polled boolean was still
reporting "not running" every fifteen minutes and the thermal page was still
showing them Stopped. The alarm list said the hall was fine and the same
platform, on the same screen, said three units were down. Ageing out is for
what nothing can measure; a state-backed alarm can be measured, so it is
excluded from the timer whether the state clears it or holds it.
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

#: Conditions the platform also POLLS as a boolean: {alarm_type: (metric key,
#: instance, the value that means the condition HOLDS)}.
#:
#: The instance is load-bearing. One boolean metric carries every alarm point
#: on a machine - a CRAH publishes alarm_state four times over, once per point
#: - so without it "is this alarm still true" would be answered by whichever
#: of its siblings was written last.
#:
#: Deliberately short. Every entry is a condition this fleet actually polls and
#: this reconciliation can therefore decide; a guess here would be worse than
#: the timer it replaces, because it would clear real alarms with confidence.
#: `*` as the instance means "whatever instance this metric has on this
#: device". A stopped machine is published under a different point name on
#: every plant type - Unit_Running on a CRAH and a CDU, Chiller_Running,
#: Run_Status on a pump, Fan_Status on a tower cell, Status_Modulating on a
#: valve - and naming one of them left the other five unshielded. It is safe
#: HERE and not in general: the machines that raise this condition publish one
#: run-status point each, while an ATS or an MCC publishes three and is not a
#: type this alarm is ever raised for.
ANY_INSTANCE = "*"

STATE_BACKED: dict[str, tuple[str, str, bool]] = {
    # A stopped machine, on any plant type that publishes its run status.
    "plant_unit_stopped":     ("equipment_state", ANY_INSTANCE, False),
    # Vendor alarm points. Each is one instance of one boolean metric, and each
    # has a trap that names it - which is what makes the poll and the trap two
    # views of one condition rather than two conditions.
    "chiller_high_pressure":  ("alarm_state", "Alarm_HighPressure", True),
    "chiller_flow_loss":      ("alarm_state", "Alarm_FlowLoss", True),
    "chiller_low_evap_temp":  ("alarm_state", "Alarm_LowEvapTemp", True),
    "tower_high_vibration":   ("alarm_state", "Alarm_HighVibration", True),
    "tower_low_basin":        ("alarm_state", "Alarm_LowBasin", True),
    "pump_fault":             ("alarm_state", "Alarm_PumpFault", True),
    "pump_low_flow":          ("alarm_state", "Alarm_LowFlow", True),
    "valve_actuator_fault":   ("alarm_state", "Alarm_ActuatorFault", True),
    "crah_airflow_loss":      ("alarm_state", "Alarm_AirflowLoss", True),
    "crah_high_temp":         ("alarm_state", "Alarm_HighTemp", True),
    "crah_filter_dirty":      ("alarm_state", "Filter_Dirty", True),
    # The generic name still carries every point that has no trap of its own -
    # phase loss, battery fault, a CDU's leak - and those file under the point
    # they came from, so the alarm's OWN instance is the one to read.
    "equipment_alarm":        ("alarm_state", ANY_INSTANCE, True),
}

#: How fresh a boolean must be to speak for the condition.
#:
#: Booleans are stored on CHANGE plus a heartbeat rather than on every poll
#: (app.ingest.changelog), and the heartbeat on this fleet runs about every
#: fifteen minutes - so twice that is the shortest window that cannot mistake
#: "between heartbeats" for "nothing to say". Older than this and the state is
#: not evidence either way, and the alarm is left alone.
STATE_FRESH_S = 1800

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
        SELECT a.id, a.device_id, a.alarm_type, a.instance,
               a.severity::text AS severity,
               d.name AS device_name, a.last_seen,
               coalesce(r.metric_key, a.metric_key)   AS metric_key,
               coalesce(r.operator, '>')              AS operator,
               -- CAST is load-bearing. asyncpg sends parameters typed, and
               -- Postgres infers this one from the integer literal beside it:
               -- `1 - :margin` types the parameter int4, which turns 0.05 into
               -- 0 and silently deletes the margin. It cleared alarms at
               -- exactly their raise threshold for two live runs while every
               -- test passed - including running the SQL by hand through psql,
               -- which sends parameters untyped and infers numeric.
               coalesce(r.clear_threshold,
                        a.threshold * (1 - CAST(:margin AS numeric)))
                                                     AS clear_threshold,
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
# The polled STATE answers, in either direction.
#
# `held` is the newest boolean for the point this alarm is about, per device,
# within the freshness window. Two uses, and the second is the one that
# matters: it CLEARS an alarm whose machine is running again and whose clear
# trap never arrived, and it SHIELDS an alarm whose machine is still stopped
# from being aged out by a timer that cannot see the state at all.
_STATE = text("""
    WITH point AS (
        SELECT * FROM unnest(
            CAST(:types AS text[]), CAST(:metrics AS text[]),
            CAST(:instances AS text[]), CAST(:holds AS boolean[])
        ) AS t(alarm_type, metric_key, instance, holds)
    ),
    latest AS (
        SELECT DISTINCT ON (tb.device_id, m.key, tb.instance)
               tb.device_id, m.key AS metric_key, tb.instance,
               tb.value, tb.ts
          FROM telemetry_bool tb
          JOIN metric m ON m.id = tb.metric_id
         WHERE tb.ts > now() - make_interval(secs => :fresh_s)
         ORDER BY tb.device_id, m.key, tb.instance, tb.ts DESC
    )
    -- `instance` is the ALARM's, because that is the key a clear has to name.
    -- Selecting the point's instead shipped a clear addressed to '*', the
    -- wildcard this map uses for "whatever the run point is called on this
    -- machine" - which matches no alarm row anywhere. The point's own name is
    -- carried separately, for the sentence that explains the clear.
    SELECT a.id::text AS id, a.device_id::text AS device_id, d.name AS device_name,
           a.alarm_type, a.instance, a.severity::text AS severity,
           p.metric_key, l.instance AS point, l.value AS state,
           (l.value IS DISTINCT FROM p.holds) AS ended
      FROM alarm a
      JOIN device d ON d.id = a.device_id
      JOIN point p  ON p.alarm_type = a.alarm_type
      JOIN latest l ON l.device_id = a.device_id
                   AND l.metric_key = p.metric_key
                   -- A named point, or - for a condition published under a
                   -- different point name on every machine - whichever
                   -- instance of that metric this alarm is filed under, and
                   -- failing that the only one the device has.
                   AND (l.instance = p.instance
                     OR (p.instance = :any_instance
                         AND (l.instance = a.instance OR a.instance = '')))
     WHERE a.state <> 'CLEARED'
       AND a.source = ANY(:sources)
""")


_AGED_OUT = text("""
    WITH candidate AS (
        SELECT a.id, a.device_id, a.alarm_type, a.instance,
               a.severity::text AS severity, d.name AS device_name,
               extract(epoch FROM (now() - a.last_seen)) AS quiet_s
          FROM alarm a
          JOIN device d ON d.id = a.device_id
         WHERE a.state <> 'CLEARED'
           AND a.source = ANY(:sources)
           AND a.last_seen < now() - make_interval(secs => :grace_s)
           -- Only what nothing can MEASURE. An alarm that CAN be decided by a
           -- reading is decided by the query above, and ageing it out on a
           -- timer would pre-empt the better answer.
           --
           -- The test is "can measured_clear act on this", so it has to be the
           -- exact complement of that query's own gate - a limit from a rule,
           -- or a limit the device sent. Testing metric_key instead left a hole
           -- between the two: a trap that names a metric but carries no limit
           -- and has no backing rule was excluded HERE for naming a metric and
           -- excluded THERE for having no limit, so nothing could ever close it.
           -- outlet_current_high sat open for over ninety minutes that way,
           -- with the strip healthy and reporting the whole time. The timer is
           -- the last resort it was always meant to be - but a last resort has
           -- to actually catch what falls past everything else.
           AND a.threshold IS NULL
           -- A state-backed condition is decided by its boolean, above, in
           -- whichever direction the boolean points. Three CRAHs aged out on
           -- this timer while the platform was still polling them "not
           -- running" every heartbeat.
           AND NOT (a.alarm_type = ANY(:state_types))
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
           c.alarm_type, c.instance, c.severity, round(c.quiet_s) AS quiet_s
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
        "state_types": list(STATE_BACKED),
    })).mappings().all()
    return [dict(r) for r in rows]


async def state_settled(session: AsyncSession, *,
                        fresh_s: int = STATE_FRESH_S) -> list[dict[str, Any]]:
    """State-backed alarms whose polled boolean says the condition ended.

    The positive half of the same read that shields a still-true alarm from
    the timer. A machine that is running again is not an alarm, however its
    clear trap fared on the wire.
    """
    if not STATE_BACKED:
        return []
    rows = (await session.execute(_STATE, {
        "sources": list(RECONCILABLE_SOURCES), "fresh_s": fresh_s,
        "any_instance": ANY_INSTANCE,
        "types": list(STATE_BACKED),
        "metrics": [v[0] for v in STATE_BACKED.values()],
        "instances": [v[1] for v in STATE_BACKED.values()],
        "holds": [v[2] for v in STATE_BACKED.values()],
    })).mappings().all()
    return [dict(r) for r in rows if r["ended"]]


def state_reason(row: dict[str, Any]) -> str:
    return (f"{row['metric_key']}/{row['point']} now reads "
            f"{'true' if row['state'] else 'false'}, so the condition is over "
            "and the clear was probably lost in transit")


def measured_reason(row: dict[str, Any]) -> str:
    whose = "the rule's" if row.get("from_rule") else "the device's own"
    return (f"{row['metric_key']} is back past {whose} clear point "
            f"({round(float(row['worst']), 1)} against "
            f"{round(float(row['clear_threshold']), 1)}) and no clear arrived")


def aged_reason(row: dict[str, Any]) -> str:
    return (f"not re-asserted for {int(row['quiet_s']) // 60} min while the "
            f"device kept reporting; the clear was probably lost in transit")
