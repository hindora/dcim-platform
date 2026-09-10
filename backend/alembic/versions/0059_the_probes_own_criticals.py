"""The probe conditions the platform had no line of its own for.

Revision ID: 0059
Revises: 0058

Rack probes were already ruled: ambient_temp_high at 27 C and humidity_high at
70 %, both on the platform's own ASHRAE lines. What they had no rule for is the
step above, the one the probes themselves annunciate - 38 C and 80 % - so those
two conditions could only ever arrive as a trap, and a lost datagram was
silence.

Thresholds are the DEVICE's here, not a line of ours: 38 C and 80 % are what
the probes fire at, and inventing a different number for the same condition
would mean the trap and the rule disagreed about when it started. Cleared at 35
and 75, which is the laddering the probes use - the critical drops first and
the major below it stays up until the aisle is genuinely back in band, rather
than both clearing at once and implying the hall recovered faster than it did.

Two dwell samples at the probes' 120 s cadence, so a single spike is not a
call-out and a real excursion is announced inside five minutes.

Not here, for want of a signal rather than for want of a rule: the mid-rack and
exhaust conditions need a three-channel probe and every probe fitted is a
single-channel part, and the airflow conditions need an airflow reading that no
probe in this estate publishes. Both would be rules watching a metric nothing
writes.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

RULES = [
    ("ambient-temp-critical", "ambient_temp_critical", "ambient_temperature",
     ">", 38.0, 35.0, "CRITICAL", "Ambient {value} C above {threshold} C"),
    ("humidity-critical", "humidity_critical", "relative_humidity",
     ">", 80.0, 75.0, "CRITICAL", "Relative humidity {value}% above {threshold}%"),
]

UPSERT = sa.text("""
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


def upgrade() -> None:
    for name, alarm_type, metric, op_, thr, clear, sev, msg in RULES:
        op.execute(UPSERT.bindparams(
            name=name, alarm_type=alarm_type, metric=metric, op=op_,
            threshold=thr, clear_threshold=clear, severity=sev, message=msg))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)")
               .bindparams(names=[r[0] for r in RULES]))
