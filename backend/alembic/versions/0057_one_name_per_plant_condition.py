"""Every plant alarm point is polled under the SAME name its trap uses.

Revision ID: 0057
Revises: 0056

The vendor alarm points were already polled - `equipment-alarm-major` and
`-warning` watch alarm_state across every device type - so a lost trap was
never going to hide a chiller's high pressure or a CRAH's airflow loss. What
was hidden is that the poll and the trap called them different things.

Measured here: CRAH1-DC1-HA-R9-01 raising crah_airflow_loss from a trap and
equipment_alarm from the poll, same device, same minute, twelve such pairs in
thirty days. The alarm key is (device, alarm_type, instance), so those are two
different alarms as far as the platform is concerned: two rows on the console,
two things to acknowledge, and reconciliation unable to see that one of them
answers for the other.

So each point that a trap already names gets a rule under that name, with the
severity the trap declares, scoped to the device types the trap covers. The
trap and the poll now land on one alarm - the trap first when it arrives, the
poll when it does not.

The generic rules keep everything else. Electrical points (phase loss, battery
fault, rectifier fault, failure to transfer) have no specific type of their
own, and equipment_alarm remains exactly right for them - so those instances
are left where they are, and only the points that now have a name of their own
are removed from the generic lists.

Deliberately NOT here: a polled raise for plant_unit_stopped on chillers,
pumps, tower cells and valves. Eighteen of this estate's forty-two plant
machines are reading "not running" as I write this, every one of them a
healthy standby, and a rule that alarmed on that would report N+1 redundancy
as a fault every day of the year. CRAHs and CDUs run whenever the hall is
live, which is why 0056 covers those two and stops there. The others keep the
trap for the raise and the polled state for the clear and the shield.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

#: (rule name, alarm type, point, severity, device types)
#:
#: Every row mirrors a trap in contracts/mappings/snmp/traps.yaml: same name,
#: same severity, same device types. The two must agree, because the whole
#: point is that they produce ONE alarm.
POINTS = [
    ("chiller-high-pressure-polled", "chiller_high_pressure",
     "Alarm_HighPressure", "CRITICAL", ["chiller"]),
    ("chiller-flow-loss-polled", "chiller_flow_loss",
     "Alarm_FlowLoss", "CRITICAL", ["chiller", "pump"]),
    ("chiller-low-evap-temp-polled", "chiller_low_evap_temp",
     "Alarm_LowEvapTemp", "MAJOR", ["chiller"]),
    ("tower-high-vibration-polled", "tower_high_vibration",
     "Alarm_HighVibration", "MAJOR", ["cooling_tower"]),
    ("tower-low-basin-polled", "tower_low_basin",
     "Alarm_LowBasin", "MAJOR", ["cooling_tower"]),
    ("pump-fault-polled", "pump_fault",
     "Alarm_PumpFault", "MAJOR", ["pump"]),
    ("pump-low-flow-polled", "pump_low_flow",
     "Alarm_LowFlow", "MAJOR", ["pump"]),
    ("valve-actuator-fault-polled", "valve_actuator_fault",
     "Alarm_ActuatorFault", "CRITICAL", ["valve"]),
    ("crah-airflow-loss-polled", "crah_airflow_loss",
     "Alarm_AirflowLoss", "MAJOR", ["crah"]),
    ("crah-high-temp-polled", "crah_high_temp",
     "Alarm_HighTemp", "MAJOR", ["crah", "cdu"]),
    ("crah-filter-dirty-polled", "crah_filter_dirty",
     "Filter_Dirty", "MINOR", ["crah"]),
]

# Points with no trap of their own - a CDU's leak and high supply temp among
# them - are deliberately absent. Nothing names them but the generic rule, so
# there is no second name to converge with, and equipment_alarm is already
# raising and clearing them from the poll.

#: Points that now have a rule of their own, and must therefore stop being
#: raised a second time under the generic name.
CLAIMED = sorted({p[2] for p in POINTS})

UPSERT = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        raise_on, instances, dwell_samples, clear_dwell_samples, severity,
        message_tpl, category, detection)
    VALUES (
        :name, :alarm_type, true, CAST(:device_types AS text[]),
        'alarm_state', 'boolean',
        true, ARRAY[:point], 1, 1, CAST(:severity AS severity_t),
        :message, 'cooling', 'state')
    ON CONFLICT (name) DO UPDATE SET
        enabled             = EXCLUDED.enabled,
        alarm_type          = EXCLUDED.alarm_type,
        device_types        = EXCLUDED.device_types,
        metric_key          = EXCLUDED.metric_key,
        metric_kind         = EXCLUDED.metric_kind,
        raise_on            = EXCLUDED.raise_on,
        instances           = EXCLUDED.instances,
        dwell_samples       = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        message_tpl         = EXCLUDED.message_tpl,
        category            = EXCLUDED.category,
        detection           = EXCLUDED.detection
""")


def upgrade() -> None:
    for name, alarm_type, point, severity, dtypes in POINTS:
        op.execute(UPSERT.bindparams(
            name=name, alarm_type=alarm_type, point=point, severity=severity,
            device_types=dtypes,
            # The point names itself in the message the same way the generic
            # rule does, so a console reads the same whichever raised it.
            message="{point} reported by the equipment"))

    # One point, one name. A generic rule still holding a claimed instance
    # would raise the same condition twice under two types, which is the
    # duplicate this migration exists to end.
    op.execute(sa.text("""
        UPDATE alarm_rule
           SET instances = ARRAY(
                   SELECT unnest(instances)
                   EXCEPT SELECT unnest(CAST(:claimed AS text[])))
         WHERE alarm_type = 'equipment_alarm'
    """).bindparams(claimed=CLAIMED))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)")
               .bindparams(names=[p[0] for p in POINTS]))
    # The generic rules take their points back, or the conditions stop being
    # watched at all.
    op.execute(sa.text("""
        UPDATE alarm_rule
           SET instances = ARRAY(
                   SELECT DISTINCT unnest(instances || CAST(:claimed AS text[])))
         WHERE name = 'equipment-alarm-major'
    """).bindparams(claimed=CLAIMED))
