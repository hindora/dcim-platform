"""Equipment publishes its own faults; start listening to them.

Phase 2 of docs/18-alert-taxonomy.md. Thirty-six distinct binary points arrive
from BACnet and Modbus on the metric `alarm_state` - 38 (equipment role, point)
pairs, since Battery_Fault and Alarm_High_Temp appear on two kinds of gear -
carrying the point name as the instance - Alarm_Leak on a CDU, Battery_Fault on a UPS, Fail_To_Transfer on an
ATS. Every one of them has been stored and never evaluated, because the engine
compared floats and no rule covered the metric. The plant has been telling us
about its own faults into a void.

Three things this needs, and they are the three columns added here:

* `metric_kind` - a binary point has no threshold to cross, only a state to be
  in, so the evaluator needs to know which kind of rule it is holding.
* `raise_on` - which value is the fault. True for an alarm point that asserts
  on fault; False for a run-status point where NOT running is the fault.
* `instances` - the points all share one metric, so severity has to be assigned
  per point. A leak and a dirty filter are not the same call-out.

**The rules are seeded DISABLED.** Three points assert intermittently on the
energy monitors - Alarm_SensorFault, Alarm_Undervoltage and Alarm_UnderFrequency
were true in roughly 0.2% of samples over the last day - so enabling them in the
same step as introducing them would mix "does classification work" with "is this
alarm real", and the first thing an operator would see is alarms appearing out
of a migration. Enable deliberately, watch the rate, then keep them. Whether
those three actually raise depends on the dwell: they need two consecutive
asserted samples, and single-sample flickers are exactly what dwell is for.

Deliberately NOT covered: `equipment_state`. A staged-off chiller or a standby
pump is not running BY DESIGN, and a rule that raises on "not running" would
alarm the entire redundant half of the plant. Lead/lag awareness is a separate
piece of work, not a boolean.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# The severity split lives in app/core/equipment_points.py so the rules and the
# tests read one list. A point in neither is a point that streams in and raises
# nothing - the condition this migration exists to end.
from app.core.equipment_points import MAJOR_POINTS, WARNING_POINTS  # noqa: E402

_INSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, severity,
                            dwell_samples, clear_dwell_samples, device_types,
                            message_tpl, enabled, metric_kind, raise_on,
                            instances, detection)
    VALUES (:name, :alarm_type, 'alarm_state', CAST(:severity AS severity_t),
            :dwell, :clear_dwell, '{}', :message_tpl, false, 'boolean', true,
            :instances, 'state')
    ON CONFLICT (name) DO UPDATE SET
        alarm_type = EXCLUDED.alarm_type,
        metric_key = EXCLUDED.metric_key,
        severity = EXCLUDED.severity,
        dwell_samples = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        message_tpl = EXCLUDED.message_tpl,
        metric_kind = EXCLUDED.metric_kind,
        raise_on = EXCLUDED.raise_on,
        instances = EXCLUDED.instances,
        detection = EXCLUDED.detection
""")


def upgrade() -> None:
    op.add_column("alarm_rule", sa.Column("metric_kind", sa.Text(),
                                          nullable=False,
                                          server_default="numeric"))
    op.add_column("alarm_rule", sa.Column("raise_on", sa.Boolean(),
                                          nullable=False,
                                          server_default=sa.true()))
    op.add_column("alarm_rule", sa.Column("instances", sa.ARRAY(sa.Text()),
                                          nullable=False,
                                          server_default="{}"))

    conn = op.get_bind()

    # Dwell 2, not 3: these points are polled slowly and a BMS asserts them
    # deliberately, so a single sample is thin evidence but three is a long
    # wait on a leak. Clear dwell 2 for the same reason in reverse - a point
    # that de-asserts once may be chattering.
    conn.execute(_INSERT, [
        {"name": "equipment-alarm-major", "alarm_type": "equipment_alarm",
         "severity": "MAJOR", "dwell": 2, "clear_dwell": 2,
         "message_tpl": "{point} reported by the equipment",
         "instances": MAJOR_POINTS},
        {"name": "equipment-alarm-warning", "alarm_type": "equipment_alarm",
         "severity": "WARNING", "dwell": 2, "clear_dwell": 2,
         "message_tpl": "{point} reported by the equipment",
         "instances": WARNING_POINTS},
    ])


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        DELETE FROM alarm_rule
         WHERE name IN ('equipment-alarm-major', 'equipment-alarm-warning')
    """))
    op.drop_column("alarm_rule", "instances")
    op.drop_column("alarm_rule", "raise_on")
    op.drop_column("alarm_rule", "metric_kind")
