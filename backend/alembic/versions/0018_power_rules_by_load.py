"""Power alarms measured against the device's own rating, not a fixed 45 kW.

`power-draw-high` was seeded with `device_types = []` - every type - and a
threshold of 45000 W. That is a rack-scale number applied to a fleet where a
switchgear metering a whole bus reads 156 kW by design, so the rule was true
from the moment it was seeded and stayed true: 20 open MAJOR alarms with
occurrence counts in the thousands, on gear that was never in trouble. The same
devices report `load_pct` between 0 and 7.5 % over the same window, which is
what "not in trouble" looks like when you ask the right question.

Three changes, one per cause:

* The absolute rule is disabled. An overload is a share of a rating, and 45 kW
  is simultaneously far above what a server can draw and far below what a
  switchgear carries, so no single watt figure can serve both.
* `power-load-high` replaces it on `load_pct`, which the electrical plant
  already publishes: 90 % raise, 80 % clear. Device types are the distribution
  family; types that do not publish the metric simply never match.
* Alarms raised by the retired rule are cleared here. A disabled rule is never
  evaluated again, so it cannot clear its own alarms - leaving them would pin
  twenty permanent MAJORs to the estate that no operator action could remove.

The new rule is device-total only. An energy monitor publishes its total AND
its branch circuits, so one overload was raising three alarms - the device, and
Ckt01 and Ckt02 that add up to it.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# Types that carry a whole feed rather than one load. Not all of them publish
# load_pct today; listing them is harmless (a rule with no matching samples
# never fires) and means the cover arrives with the metric rather than after
# somebody notices it is missing.
_DISTRIBUTION = [
    "ups", "switchgear", "utility_feed", "ats", "generator", "mcc", "mpp",
    "pdu", "rpp", "energy_monitor",
]


def upgrade() -> None:
    op.add_column("alarm_rule",
                  sa.Column("device_total_only", sa.Boolean(), nullable=False,
                            server_default=sa.false()))

    conn = op.get_bind()

    conn.execute(sa.text("""
        UPDATE alarm_rule SET enabled = false WHERE name = 'power-draw-high'
    """))

    # Clear what it raised. `cleared_by` records why, so the history explains
    # itself to whoever finds these later.
    conn.execute(sa.text("""
        UPDATE alarm
           SET state = 'CLEARED',
               cleared_at = now(),
               cleared_by = 'migration:0018 absolute power rule retired'
         WHERE alarm_type = 'power_draw_high' AND state <> 'CLEARED'
    """))

    conn.execute(sa.text("""
        INSERT INTO alarm_rule (name, alarm_type, metric_key, operator, threshold,
                                clear_threshold, dwell_samples, clear_dwell_samples,
                                severity, device_types, message_tpl,
                                device_total_only)
        VALUES ('power-load-high', 'power_load_high', 'load_pct', '>', 90, 80, 3, 2,
                CAST('MAJOR' AS severity_t), :types,
                'Load {value}% of rated capacity, above {threshold}%', true)
        ON CONFLICT (name) DO UPDATE SET
            alarm_type = EXCLUDED.alarm_type,
            metric_key = EXCLUDED.metric_key,
            operator = EXCLUDED.operator,
            threshold = EXCLUDED.threshold,
            clear_threshold = EXCLUDED.clear_threshold,
            dwell_samples = EXCLUDED.dwell_samples,
            clear_dwell_samples = EXCLUDED.clear_dwell_samples,
            severity = EXCLUDED.severity,
            device_types = EXCLUDED.device_types,
            message_tpl = EXCLUDED.message_tpl,
            device_total_only = EXCLUDED.device_total_only,
            enabled = true
    """), {"types": _DISTRIBUTION})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM alarm_rule WHERE name = 'power-load-high'"))
    conn.execute(sa.text("""
        UPDATE alarm_rule SET enabled = true WHERE name = 'power-draw-high'
    """))
    # The alarms cleared above are not resurrected: they were false, and an
    # alarm's history is a record of what operators saw, not a value to restore.
    op.drop_column("alarm_rule", "device_total_only")
