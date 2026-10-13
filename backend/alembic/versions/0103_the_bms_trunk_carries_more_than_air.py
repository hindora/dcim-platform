"""The BMS trunk carries more than air and water.

docs/27 §8b.3 and §8c.2, the simulator's S3 increment 2. Five instruments a
real site wires to its BMS and never networks arrive on the same RS-485 trunk
as the thermowells and room transmitters:

  door contact      one bit; opening is normal, a door HELD open is the fault
  hydrogen          % of the lower explosive limit in the battery room
  battery string    its own temperature, not the room's
  bulk diesel       level and litres in the yard's tank
  makeup water      level and litres in the towers' tank

Two things change here. The metric table is re-synced from the registry
(mechanism of 0004/0008/0016/0062 verbatim) so the ingest worker accepts the
new keys. And the rules that make them alarms are seeded - thresholds the
standards and the equipment use, not lines of ours:

  hydrogen     10 % LEL major, 25 % LEL critical. IEEE 1635 / NFPA 1 cap a
               VRLA room at 1 % by volume, which is 25 % LEL; detectors ship
               with the first alarm at 10.
  string temp  30 C major, 35 C critical. VRLA life halves per ~10 K above
               25 C; 35 C sustained is where thermal runaway starts to be a
               question rather than a cost.
  bulk fuel    30 % major, 15 % critical - the reorder level and the point
               at which a delivery is late.
  makeup water 40 % major, 20 % critical, on a tank sized for a day or two.
  door         held open for two minutes, minor. A boolean rule with a time
               dwell: a technician walking in is not an alarm.

Every rule is scoped to `sensor`, so a generator's own day-tank rule and a
UPS's own string temperature are untouched.

Revision ID: 0103
Revises: 0102
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None

UPSERT_METRIC = sa.text("""
    INSERT INTO metric (key, display_name, unit, value_type, aggregation,
                        min_valid, max_valid, stale_after_s, is_hot)
    VALUES (:key, :display_name, :unit, :value_type, :aggregation,
            :min_valid, :max_valid, :stale_after_s, :is_hot)
    ON CONFLICT (key) DO UPDATE
      SET display_name = EXCLUDED.display_name,
          unit = EXCLUDED.unit,
          value_type = EXCLUDED.value_type,
          aggregation = EXCLUDED.aggregation,
          min_valid = EXCLUDED.min_valid,
          max_valid = EXCLUDED.max_valid,
          stale_after_s = EXCLUDED.stale_after_s,
          is_hot = EXCLUDED.is_hot,
          deprecated_at = NULL
""")

# name, alarm_type, metric, operator, threshold, clear, severity, message
THRESHOLD_RULES = [
    ("hydrogen-high", "hydrogen_high", "hydrogen_lel_pct", ">", 10.0, 8.0, "MAJOR",
     "Hydrogen {value} % LEL above {threshold} % LEL"),
    ("hydrogen-critical", "hydrogen_critical", "hydrogen_lel_pct", ">", 25.0, 20.0, "CRITICAL",
     "Hydrogen {value} % LEL above {threshold} % LEL - evacuate and ventilate"),
    ("battery-string-temp-high", "battery_temp_high", "battery_temperature", ">", 30.0, 28.0, "MAJOR",
     "Battery string {value} C above {threshold} C"),
    ("battery-string-temp-critical", "battery_temp_critical", "battery_temperature", ">", 35.0, 32.0, "CRITICAL",
     "Battery string {value} C above {threshold} C - thermal runaway risk"),
    ("bulk-fuel-low", "bulk_fuel_low", "fuel_level_pct", "<", 30.0, 35.0, "MAJOR",
     "Bulk fuel {value}% below {threshold}%"),
    ("bulk-fuel-critical", "bulk_fuel_critical", "fuel_level_pct", "<", 15.0, 20.0, "CRITICAL",
     "Bulk fuel {value}% below {threshold}% - delivery overdue"),
    ("makeup-water-low", "makeup_water_low", "water_level_pct", "<", 40.0, 45.0, "MAJOR",
     "Makeup water {value}% below {threshold}%"),
    ("makeup-water-critical", "makeup_water_critical", "water_level_pct", "<", 20.0, 25.0, "CRITICAL",
     "Makeup water {value}% below {threshold}% - tower makeup at risk"),
]

UPSERT_THRESHOLD = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        operator, threshold, clear_threshold, dwell_samples,
        clear_dwell_samples, severity, message_tpl, detection)
    VALUES (
        :name, :alarm_type, true, ARRAY['sensor'], :metric, 'numeric',
        :op, :threshold, :clear_threshold, 2, 2,
        CAST(:severity AS severity_t), :message, 'threshold')
    ON CONFLICT (name) DO UPDATE SET
        enabled             = EXCLUDED.enabled,
        alarm_type          = EXCLUDED.alarm_type,
        device_types        = EXCLUDED.device_types,
        metric_key          = EXCLUDED.metric_key,
        metric_kind         = EXCLUDED.metric_kind,
        operator            = EXCLUDED.operator,
        threshold           = EXCLUDED.threshold,
        clear_threshold     = EXCLUDED.clear_threshold,
        dwell_samples       = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        message_tpl         = EXCLUDED.message_tpl,
        detection           = EXCLUDED.detection
""")

# The door: a boolean rule with a time dwell. Asserted for two minutes is
# "held open"; one sample is somebody walking in.
UPSERT_DOOR = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        raise_on, dwell_samples, dwell_seconds, clear_dwell_samples,
        severity, message_tpl, detection)
    VALUES (
        'door-held-open', 'door_held_open', true, ARRAY['sensor'], 'door_open', 'boolean',
        true, 2, 120, 1, CAST('MINOR' AS severity_t), 'Door held open', 'state')
    ON CONFLICT (name) DO UPDATE SET
        enabled             = EXCLUDED.enabled,
        alarm_type          = EXCLUDED.alarm_type,
        device_types        = EXCLUDED.device_types,
        metric_key          = EXCLUDED.metric_key,
        metric_kind         = EXCLUDED.metric_kind,
        raise_on            = EXCLUDED.raise_on,
        dwell_samples       = EXCLUDED.dwell_samples,
        dwell_seconds       = EXCLUDED.dwell_seconds,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        message_tpl         = EXCLUDED.message_tpl,
        detection           = EXCLUDED.detection
""")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(UPSERT_METRIC, [
        {"key": m.key, "display_name": m.display_name, "unit": m.unit,
         "value_type": m.value_type, "aggregation": m.aggregation,
         "min_valid": m.min_valid, "max_valid": m.max_valid,
         "stale_after_s": m.stale_after_s, "is_hot": m.hot}
        for m in METRICS.values()
    ])
    conn.execute(sa.text("""
        UPDATE metric SET deprecated_at = now()
        WHERE deprecated_at IS NULL AND key <> ALL(:keys)
    """), {"keys": list(METRICS.keys())})
    for name, alarm_type, metric, op_, thr, clear, sev, msg in THRESHOLD_RULES:
        op.execute(UPSERT_THRESHOLD.bindparams(
            name=name, alarm_type=alarm_type, metric=metric, op=op_,
            threshold=thr, clear_threshold=clear, severity=sev, message=msg))
    op.execute(UPSERT_DOOR)


def downgrade() -> None:
    names = [r[0] for r in THRESHOLD_RULES] + ["door-held-open"]
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)").bindparams(names=names))
    # The registry sync has no inverse: dropping metrics would orphan telemetry.
