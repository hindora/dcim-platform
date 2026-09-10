"""Every electrical alarm point is polled under the SAME name its trap uses.

Revision ID: 0060
Revises: 0059

0057 did this for the plant. The electrical estate has the identical split: the
UPS, generator, transfer switch and energy monitors publish their alarm points
as booleans, the generic equipment_alarm rules poll every one of them, and the
traps that announce the same conditions arrive under names of their own. One
failure, two rows, and reconciliation unable to see that either answers for the
other.

Fourteen points, each verified against contracts/mappings/snmp/traps.yaml for
its name and its severity before it was written here:

  UPS         low battery, battery, charger, rectifier and phase faults
  Generator   high temperature, low fuel, low coolant, battery fault
  Transfer    not in auto, failure to transfer
  Metering    voltage imbalance, undervoltage, under-frequency

Points deliberately left with the generic rule, because no trap names them and
there is therefore no second name to converge with: overcurrent, phase loss,
high THD, sensor fault, the generator's transfer alarm, and every equipment
state point on switchgear, MCC, MPP and the utility feed.

Severity is the trap's, not a fresh judgement: a platform that graded the same
condition differently from the device that reported it would be arguing with
its own evidence. Dwell of one in both directions, as with the plant points: a
boolean is written when it CHANGES, so the first sample IS the event, and
waiting for a second means waiting for the next heartbeat.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

#: (rule name, alarm type, point, severity, device types)
POINTS = [
    ("ups-battery-low-polled", "ups_battery_low",
     "Low_Battery", "CRITICAL", ["ups"]),
    ("ups-battery-failure-polled", "battery_failure",
     "Battery_Fault", "CRITICAL", ["ups"]),
    ("ups-charger-failure-polled", "charger_failure",
     "Charger_Fault", "CRITICAL", ["ups"]),
    ("ups-rectifier-failure-polled", "rectifier_failure",
     "Rectifier_Fault", "CRITICAL", ["ups"]),
    ("ups-phase-failure-polled", "ups_phase_failure",
     "Phase_Fault", "CRITICAL", ["ups"]),
    ("generator-temp-high-polled", "generator_temp_high",
     "Alarm_High_Temp", "MAJOR", ["generator"]),
    ("generator-low-fuel-polled", "generator_low_fuel",
     "Alarm_Low_Fuel", "MAJOR", ["generator"]),
    ("generator-low-coolant-polled", "generator_low_coolant",
     "Alarm_Low_Coolant", "MAJOR", ["generator"]),
    ("generator-battery-failure-polled", "generator_battery_failure",
     "Battery_Fault", "CRITICAL", ["generator"]),
    ("ats-not-in-auto-polled", "ats_not_in_auto",
     "Not_In_Auto", "MINOR", ["ats"]),
    ("ats-fail-to-transfer-polled", "ats_fail_to_transfer",
     "Fail_To_Transfer", "CRITICAL", ["ats"]),
    ("meter-phase-imbalance-polled", "phase_imbalance",
     "Alarm_VoltageImbalance", "MAJOR", ["energy_monitor"]),
    ("meter-undervoltage-polled", "input_voltage_low",
     "Alarm_Undervoltage", "MAJOR", ["energy_monitor"]),
    ("meter-under-frequency-polled", "frequency_out_of_range",
     "Alarm_UnderFrequency", "MAJOR", ["energy_monitor"]),
]

#: Points now claimed by a rule of their own. A generic rule still holding one
#: would raise the same condition twice under two names, which is the duplicate
#: this migration exists to end. Battery_Fault is claimed on the UPS and on the
#: generator by two different alarm types, and removing it from the generic
#: list is right for both.
CLAIMED = sorted({p[2] for p in POINTS})

UPSERT = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        raise_on, instances, dwell_samples, clear_dwell_samples, severity,
        message_tpl, detection)
    VALUES (
        :name, :alarm_type, true, CAST(:device_types AS text[]),
        'alarm_state', 'boolean',
        true, ARRAY[:point], 1, 1, CAST(:severity AS severity_t),
        '{point} reported by the equipment', 'state')
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
        detection           = EXCLUDED.detection
""")


def upgrade() -> None:
    for name, alarm_type, point, severity, dtypes in POINTS:
        op.execute(UPSERT.bindparams(
            name=name, alarm_type=alarm_type, point=point,
            severity=severity, device_types=dtypes))

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
    op.execute(sa.text("""
        UPDATE alarm_rule
           SET instances = ARRAY(
                   SELECT DISTINCT unnest(instances || CAST(:claimed AS text[])))
         WHERE name = 'equipment-alarm-major'
    """).bindparams(claimed=CLAIMED))
