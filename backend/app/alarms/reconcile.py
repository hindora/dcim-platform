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
    # The electrical estate, same shape: a point a trap names, polled as a
    # boolean beside it. Battery_Fault is two conditions on two machines - a
    # UPS battery and a generator's starting battery - which is why they are
    # two alarm types over one point name.
    "ups_battery_low":        ("alarm_state", "Low_Battery", True),
    "battery_failure":        ("alarm_state", "Battery_Fault", True),
    "charger_failure":        ("alarm_state", "Charger_Fault", True),
    "rectifier_failure":      ("alarm_state", "Rectifier_Fault", True),
    "ups_phase_failure":      ("alarm_state", "Phase_Fault", True),
    "generator_temp_high":    ("alarm_state", "Alarm_High_Temp", True),
    "generator_low_fuel":     ("alarm_state", "Alarm_Low_Fuel", True),
    "generator_low_coolant":  ("alarm_state", "Alarm_Low_Coolant", True),
    "generator_battery_failure": ("alarm_state", "Battery_Fault", True),
    "ats_not_in_auto":        ("alarm_state", "Not_In_Auto", True),
    "ats_fail_to_transfer":   ("alarm_state", "Fail_To_Transfer", True),
    "phase_imbalance":        ("alarm_state", "Alarm_VoltageImbalance", True),
    "input_voltage_low":      ("alarm_state", "Alarm_Undervoltage", True),
    "frequency_out_of_range": ("alarm_state", "Alarm_UnderFrequency", True),
    # The generic name still carries every point that has no trap of its own -
    # phase loss, battery fault, a CDU's leak - and those file under the point
    # they came from, so the alarm's OWN instance is the one to read.
    "equipment_alarm":        ("alarm_state", ANY_INSTANCE, True),
}

#: The same condition under another point name on another kind of machine:
#: {(alarm_type, device_type): point}. The metric and the polarity are the
#: STATE_BACKED entry's; only the instance differs.
#:
#: Each of these left a trap alarm with no way to close: the poll was read at
#: the name a different machine uses, found nothing, and a state-backed alarm
#: is shielded from the timer, so a lost clear meant forever. A pump calls its
#: fault Alarm_Fault and its flow loss Alarm_LowFlow (Alarm_PumpFault and
#: Alarm_FlowLoss are a CDU's and a chiller's); a CDU's high temperature is
#: its supply, Alarm_HighSupplyTemp, not a CRAH's Alarm_HighTemp.
STATE_POINT_BY_TYPE: dict[tuple[str, str], str] = {
    ("pump_fault", "pump"): "Alarm_Fault",
    ("chiller_flow_loss", "pump"): "Alarm_LowFlow",
    ("crah_high_temp", "cdu"): "Alarm_HighSupplyTemp",
}


def state_points() -> dict[str, list[Any]]:
    """STATE_BACKED as parallel arrays for the SQL, with a device_type column:
    '' for the general entry, a type for each STATE_POINT_BY_TYPE override."""
    rows = [(t, "", m, i, h) for t, (m, i, h) in STATE_BACKED.items()]
    for (t, dt), inst in STATE_POINT_BY_TYPE.items():
        m, _i, h = STATE_BACKED[t]
        rows.append((t, dt, m, inst, h))
    return {"types": [r[0] for r in rows], "device_types": [r[1] for r in rows],
            "metrics": [r[2] for r in rows], "instances": [r[3] for r in rows],
            "holds": [r[4] for r in rows]}


#: How far back a point or a rule's metric counts as something the device
#: PUBLISHES. Longer than any heartbeat, so a slow boolean is never mistaken
#: for one the machine does not have: only a point this device has not sent in
#: six hours lets the timer take an alarm the point would otherwise decide.
POINT_KNOWN_S = 21600

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

#: Point events, not conditions. A cold start happened and is over: there is
#: nothing still wrong and nothing that will ever send a clear, so the only
#: honest life for the alarm is a short one. Still subject to the telemetry
#: gate below - a device that restarted and then went dark again keeps it.
#:
#: Nineteen of these sat open after rack R2-02's power came back, each a
#: device that had booted normally and was polling fine, waiting out the
#: thirty-minute condition timer that was never meant for them.
EVENT_TYPES = frozenset({"device_restarted"})
EVENT_GRACE_S = 900


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
               (r.id IS NOT NULL)                     AS from_rule,
               -- One alarm type can have several rules, told apart by the
               -- instances they cover: ambient_temp_high is the 27 C rack
               -- intake on a sensor's own reading ("") and the 35 C room limit
               -- on a plant-room transmitter (ROOM). Each rule is judged on
               -- its own readings, below, so the rule and the id travel on.
               r.id                                   AS rule_id,
               r.instances                            AS rule_instances
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
        SELECT c.id, c.device_id, c.device_name, c.alarm_type, c.instance, c.severity,
               c.metric_key, c.operator, c.clear_threshold, c.need, c.from_rule,
               c.rule_id, t.value,
               -- Per alarm AND per rule. Partitioning by the alarm alone dealt
               -- each rule's readings out between the rules: with two rules on
               -- ambient_temp_high every rule saw one sample of the two it
               -- needs, nothing ever cleared, and a sensor reading 23.8 C kept
               -- a lost-clear trap alarm open for six days.
               row_number() OVER (PARTITION BY c.id, c.rule_id ORDER BY t.ts DESC) AS rn
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
                                 -- A rule speaks only for the readings it
                                 -- covers: the room limit never judges a rack
                                 -- intake. No rule, or a rule over every
                                 -- instance, takes the metric as it comes.
                                 AND (c.rule_instances IS NULL
                                      OR cardinality(c.rule_instances) = 0
                                      OR coalesce(t.instance, '') = ANY(c.rule_instances))
    ), tail AS (
        SELECT id, device_id, device_name, alarm_type, instance, severity, metric_key,
               operator, clear_threshold, need, from_rule, rule_id,
               count(*)   AS samples,
               max(value) AS hi,
               min(value) AS lo,
               (count(*) >= need
                AND ((operator = '>' AND max(value) < clear_threshold)
                  OR (operator = '<' AND min(value) > clear_threshold))) AS clear
          FROM ranked
         WHERE rn <= need
         GROUP BY id, device_id, device_name, alarm_type, instance, severity, metric_key,
                  operator, clear_threshold, need, from_rule, rule_id
    )
    -- One row per alarm, and only when no rule with readings still holds it:
    -- the 35 C room limit being clear must never close an alarm the 27 C
    -- intake limit still upholds. The instance is the one the alarm is FILED
    -- under (a trap's own, here "1"), which is what the clear has to match.
    SELECT DISTINCT ON (t.id)
           t.id::text AS id, t.device_id::text AS device_id, t.device_name,
           t.alarm_type, t.instance, t.severity, t.metric_key, t.from_rule,
           CASE WHEN t.operator = '>' THEN t.hi ELSE t.lo END AS worst,
           t.clear_threshold
      FROM tail t
     WHERE t.clear
       AND NOT EXISTS (SELECT 1 FROM tail u WHERE u.id = t.id AND NOT u.clear)
     ORDER BY t.id, t.clear_threshold
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
            CAST(:types AS text[]), CAST(:device_types AS text[]), CAST(:metrics AS text[]),
            CAST(:instances AS text[]), CAST(:holds AS boolean[])
        ) AS t(alarm_type, device_type, metric_key, instance, holds)
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
                   -- the general point, or this machine's own name for it
                   AND (p.device_type = '' OR p.device_type = d.device_type)
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
    WITH point AS (
        SELECT * FROM unnest(
            CAST(:types AS text[]), CAST(:device_types AS text[]), CAST(:metrics AS text[]),
            CAST(:instances AS text[]), CAST(:holds AS boolean[])
        ) AS t(alarm_type, device_type, metric_key, instance, holds)
    ),
    candidate AS (
        SELECT a.id, a.device_id, a.alarm_type, a.instance,
               a.severity::text AS severity, d.name AS device_name,
               extract(epoch FROM (now() - a.last_seen)) AS quiet_s
          FROM alarm a
          JOIN device d ON d.id = a.device_id
         WHERE a.state <> 'CLEARED'
           AND a.source = ANY(:sources)
           AND a.last_seen < now() - make_interval(secs =>
                   CASE WHEN a.alarm_type = ANY(:event_types)
                        THEN CAST(:event_grace_s AS double precision)
                        ELSE CAST(:grace_s AS double precision) END)
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
           --
           -- ...but only where that boolean EXISTS on this machine. A pump has
           -- no Alarm_PumpFault and a UPS no Alarm_UnderFrequency; shielding
           -- those from the timer left a lost clear open forever, because no
           -- poll could ever speak for them.
           AND NOT EXISTS (
               SELECT 1 FROM point p
                 JOIN metric m ON m.key = p.metric_key
                 JOIN telemetry_bool tb ON tb.device_id = a.device_id
                                       AND tb.metric_id = m.id
                WHERE p.alarm_type = a.alarm_type
                  AND (p.device_type = '' OR p.device_type = d.device_type)
                  AND (tb.instance = p.instance
                    OR (p.instance = :any_instance
                        AND (tb.instance = a.instance OR a.instance = '')))
                  AND tb.ts > now() - make_interval(secs => :known_s)
           )
           -- A rule that can measure it decides it - where the device
           -- actually reports the rule's metric. A rack PDU reports no
           -- temperature, so a clear rule on ambient_temperature can never
           -- speak for its pdu_temp_high and must not stand in the timer's way.
           AND NOT EXISTS (
               SELECT 1 FROM alarm_rule r
                WHERE r.alarm_type = a.alarm_type
                  AND r.enabled
                  AND r.metric_key IS NOT NULL
                  AND r.clear_threshold IS NOT NULL
                  AND (cardinality(r.device_types) = 0
                    OR d.device_type = ANY(r.device_types))
                  AND EXISTS (
                      SELECT 1 FROM telemetry_sample t
                        JOIN metric m ON m.id = t.metric_id
                       WHERE t.device_id = a.device_id
                         AND m.key = r.metric_key
                         AND (cardinality(coalesce(r.instances, '{}')) = 0
                           OR coalesce(t.instance, '') = ANY(r.instances))
                         AND t.ts > now() - make_interval(secs => :known_s)
                  )
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


#: How long a metric key must be silent before its alarm is treated as
#: unclearable rather than merely quiet.
#:
#: Two hours is twelve polls of the slowest profile in this estate (the 600 s
#: network ones) and sixty of the fastest, so a key that has not spoken in that
#: time is not being polled slowly - it has stopped existing.
ORPHAN_SILENT_S = 7200


#: An alarm on a metric key the device no longer publishes.
#:
#: The fourth situation, and the one the other three cannot reach. An alarm is
#: keyed on (device, type, INSTANCE), and an instance is a vocabulary that can
#: change under it: migration 0063 moved the plant-room transmitters from the
#: blank instance a rack probe uses to `ROOM`, so their readings started
#: landing on a different key from the alarms already open against them.
#:
#: THR-DC1-CP raised ambient_temp_high at 27.2 C against the 27 C rack-intake
#: limit on 11 September - correctly, under the rules of that day - and then
#: went silent on that key forever. The rule engine owns threshold alarms and
#: clears them from the next reading in the clear band; no such reading can
#: ever arrive. `measured_clear` skips threshold alarms for exactly that
#: reason, and `aged_out` skips anything carrying a threshold. So it sat ACTIVE
#: for five days on a healthy transmitter that was, the whole time, publishing
#: 27.7 C under `ROOM` and comfortably inside the 35 C limit its room is
#: actually held to.
#:
#: This is not second-guessing the rule engine. The engine cannot act on this
#: alarm at all - not "has not yet", but cannot, by construction - and an alarm
#: nobody can clear is an alarm nobody reads.
#:
#: The safety condition is the same one the timer uses and matters more here:
#: the DEVICE must still be delivering telemetry. "This key stopped" and "this
#: machine went dark" look identical from the alarm table, and only the first
#: means the alarm is stranded rather than true.
#:
#: Both tests are LATERAL ... LIMIT 1 on purpose: one index probe per open
#: alarm. Written as NOT EXISTS / EXISTS with the metric id looked up in a
#: correlated subquery, the planner chose a hash anti-join that read every
#: sample of the last two hours - 3.3 million rows - to answer for ONE open
#: alarm: 26-42 s a run, run by both ingest workers at once, on a host short of
#: memory. The workers process the collectors' heartbeats too, so every run
#: also made all eight collectors look stale (2026-10-27). As lookups it is
#: 3 ms.
_ORPHANED_KEY = text("""
    SELECT a.id::text AS id, a.device_id::text AS device_id,
           d.name AS device_name, a.alarm_type, a.instance,
           a.severity::text AS severity, a.metric_key,
           extract(epoch FROM (now() - a.last_seen)) AS quiet_s
      FROM alarm a
      JOIN device d ON d.id = a.device_id
      JOIN metric m ON m.key = a.metric_key
      -- Anything on this key, recently enough that slow polling would explain
      -- a gap.
      LEFT JOIN LATERAL (
          SELECT 1 AS hit FROM telemetry_sample t
           WHERE t.device_id = a.device_id
             AND t.metric_id = m.id
             AND coalesce(t.instance, '') = coalesce(a.instance, '')
             AND t.ts > now() - make_interval(secs => :silent_s)
           LIMIT 1
      ) keyed ON true
      -- A boolean lives in telemetry_bool, change-logged with a heartbeat
      -- well inside silent_s. Reading only telemetry_sample found nothing for
      -- psu_state or bank_breaker_closed EVER, so every rule alarm on a
      -- boolean was closed as "orphaned" within a minute of being raised and
      -- re-raised on the next poll (2026-10-28, psu_failure and
      -- breaker_tripped, live).
      LEFT JOIN LATERAL (
          SELECT 1 AS hit FROM telemetry_bool t
           WHERE t.device_id = a.device_id
             AND t.metric_id = m.id
             AND coalesce(t.instance, '') = coalesce(a.instance, '')
             AND t.ts > now() - make_interval(secs => :silent_s)
           LIMIT 1
      ) keyed_bool ON true
      -- Anything at all from the device.
      LEFT JOIN LATERAL (
          SELECT 1 AS hit FROM telemetry_sample t
           WHERE t.device_id = a.device_id
             AND t.ts > now() - make_interval(secs => :fresh_s)
           LIMIT 1
      ) fresh ON true
     WHERE a.state <> 'CLEARED'
       AND a.metric_key IS NOT NULL
       -- A state-backed condition is decided by its boolean wherever that
       -- points, and a boolean is change-logged: no rows for hours is what a
       -- healthy one looks like, so silence here would mean the opposite of
       -- what it means for a numeric key.
       AND NOT (a.alarm_type = ANY(:state_types))
       -- Nothing on this key, for long enough that slow polling is not the
       -- explanation...
       AND keyed.hit IS NULL
       AND keyed_bool.hit IS NULL
       -- ...while the device itself is plainly still there.
       AND fresh.hit IS NOT NULL
""")


async def orphaned_key(session: AsyncSession, *,
                       silent_s: int = ORPHAN_SILENT_S,
                       fresh_s: int = SEEING_IT_S) -> list[dict[str, Any]]:
    """Alarms whose metric key the device has stopped publishing.

    Not "no reading has contradicted it" - no reading CAN, because the key it
    was raised against no longer exists on that device. Usually an instance
    vocabulary that moved under an open alarm.
    """
    rows = (await session.execute(_ORPHANED_KEY, {
        "silent_s": silent_s, "fresh_s": fresh_s,
        "state_types": list(STATE_BACKED),
    })).mappings().all()
    return [dict(r) for r in rows]


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
        "event_types": list(EVENT_TYPES), "event_grace_s": EVENT_GRACE_S,
        "any_instance": ANY_INSTANCE, "known_s": POINT_KNOWN_S, **state_points(),
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
        "any_instance": ANY_INSTANCE, **state_points(),
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


def orphan_reason(row: dict[str, Any]) -> str:
    return (f"{row['metric_key']}"
            f"{'/' + row['instance'] if row['instance'] else ''} has not been "
            f"published for {int(row['quiet_s']) // 3600} h while the device "
            "kept reporting, so nothing can ever clear this - the metric or "
            "its instance moved after the alarm was raised")


def aged_reason(row: dict[str, Any]) -> str:
    return (f"not re-asserted for {int(row['quiet_s']) // 60} min while the "
            f"device kept reporting; the clear was probably lost in transit")
