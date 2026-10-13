"""The substation has a transformer.

docs/27 §8c.2, the simulator's S3 increment 2b. Every site of this size takes
its supply at medium voltage and steps it down on its own compound, and the
thing that fails there fails slowly and visibly: oil warms over hours, the
winding over minutes. What a DCIM polls is the monitor on the tank, which is
why the transformer is a device type with no SNMP, a Modbus template and
three keys of its own.

Three things change here. A `transformer` device type is seeded so the
importer can file the device (0003's table; ON CONFLICT so a hand-made row
survives). The metric table is re-synced from the registry (mechanism of
0004/0008/0016/0062/0103 verbatim) so the ingest worker accepts the new
keys. And the rules that make the temperatures alarms are seeded at the
numbers the monitors ship with, not lines of ours:

  top oil      85 C major, 95 C critical. IEC 60076-7 rates ageing at a
               98 C hot-spot; the tank relay is set below that so a hot day
               at full load shows before it costs life.
  winding      105 C major, 115 C critical - the WTI's alarm and trip
               stages on nearly every mineral-oil unit.
  load         the estate's power-load-high rule (0018) learns the type, so
               a transformer at 90 % of nameplate is reported the way a
               switchboard at 90 % already is.

Every rule is scoped to `transformer`, so nothing else's oil or winding -
there is none - is touched.

Revision ID: 0104
Revises: 0103
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.core.metrics_gen import METRICS

revision = "0104"
down_revision = "0103"
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
    (
        "transformer-oil-temp-high",
        "transformer_oil_temp_high",
        "oil_temperature",
        ">",
        85.0,
        80.0,
        "MAJOR",
        "Top oil {value} C above {threshold} C",
    ),
    (
        "transformer-oil-temp-critical",
        "transformer_oil_temp_critical",
        "oil_temperature",
        ">",
        95.0,
        90.0,
        "CRITICAL",
        "Top oil {value} C above {threshold} C - trip stage",
    ),
    (
        "transformer-winding-temp-high",
        "transformer_winding_temp_high",
        "winding_temperature",
        ">",
        105.0,
        100.0,
        "MAJOR",
        "Winding hot-spot {value} C above {threshold} C",
    ),
    (
        "transformer-winding-temp-critical",
        "transformer_winding_temp_critical",
        "winding_temperature",
        ">",
        115.0,
        110.0,
        "CRITICAL",
        "Winding hot-spot {value} C above {threshold} C - trip stage",
    ),
]

UPSERT_THRESHOLD = sa.text("""
    INSERT INTO alarm_rule (
        name, alarm_type, enabled, device_types, metric_key, metric_kind,
        operator, threshold, clear_threshold, dwell_samples,
        clear_dwell_samples, severity, message_tpl, detection)
    VALUES (
        :name, :alarm_type, true, ARRAY['transformer'], :metric, 'numeric',
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


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
        INSERT INTO device_type (code, display_name, category, is_rack_mounted)
        VALUES ('transformer', 'Transformer', 'power', false)
        ON CONFLICT (code) DO NOTHING
    """)
    )
    conn.execute(
        UPSERT_METRIC,
        [
            {
                "key": m.key,
                "display_name": m.display_name,
                "unit": m.unit,
                "value_type": m.value_type,
                "aggregation": m.aggregation,
                "min_valid": m.min_valid,
                "max_valid": m.max_valid,
                "stale_after_s": m.stale_after_s,
                "is_hot": m.hot,
            }
            for m in METRICS.values()
        ],
    )
    conn.execute(
        sa.text("""
        UPDATE metric SET deprecated_at = now()
        WHERE deprecated_at IS NULL AND key <> ALL(:keys)
    """),
        {"keys": list(METRICS.keys())},
    )
    for name, alarm_type, metric, op_, thr, clear, sev, msg in THRESHOLD_RULES:
        op.execute(
            UPSERT_THRESHOLD.bindparams(
                name=name,
                alarm_type=alarm_type,
                metric=metric,
                op=op_,
                threshold=thr,
                clear_threshold=clear,
                severity=sev,
                message=msg,
            )
        )
    conn.execute(
        sa.text("""
        UPDATE alarm_rule
           SET device_types = array_append(device_types, 'transformer')
         WHERE name = 'power-load-high'
           AND NOT ('transformer' = ANY(device_types))
    """)
    )


def downgrade() -> None:
    names = [r[0] for r in THRESHOLD_RULES]
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)").bindparams(names=names))
    op.execute(
        sa.text("""
        UPDATE alarm_rule
           SET device_types = array_remove(device_types, 'transformer')
         WHERE name = 'power-load-high'
    """)
    )
    # The registry sync has no inverse: dropping metrics would orphan telemetry.
    # The device type row stays for the same reason - devices may point at it.
