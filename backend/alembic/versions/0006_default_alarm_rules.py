"""Seed the default alarm rule set.

Revision ID: 0006
Revises: 0005

Every rule carries a deadband and a dwell. A rule without them raises and
clears on every sample while the metric sits on its threshold, which at this
fleet size means hundreds of alarms an hour and an operator who stops reading
the list.

The inlet thresholds are the ASHRAE A2 figures - 27 C recommended upper, 32 C
allowable - and are deliberately per-rule rather than hardcoded, because a
liquid-cooled hall and an air-cooled hall have different correct answers.

Re-runnable: upserts on rule name.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# name, alarm_type, metric, op, threshold, clear, dwell, clear_dwell, severity,
# device_types, message
RULES = [
    ("cpu-temp-high", "cpu_temp_high", "cpu_temperature", ">", 80, 75, 3, 2,
     "WARNING", ["server"], "CPU temperature {value} C above {threshold} C"),
    ("cpu-temp-critical", "cpu_temp_critical", "cpu_temperature", ">", 90, 85, 2, 2,
     "CRITICAL", ["server"], "CPU temperature {value} C critical"),
    ("cpu-high", "cpu_high", "cpu_utilization", ">", 90, 80, 5, 3,
     "WARNING", [], "CPU {value}% sustained above {threshold}%"),
    ("memory-high", "memory_high", "memory_utilization", ">", 90, 85, 5, 3,
     "WARNING", [], "Memory {value}% above {threshold}%"),
    ("inlet-temp-high", "inlet_temp_high", "inlet_temperature", ">", 27, 25, 3, 2,
     "WARNING", [], "Inlet air {value} C above the ASHRAE recommended 27 C"),
    ("inlet-temp-critical", "inlet_temp_critical", "inlet_temperature", ">", 32, 30, 2, 2,
     "CRITICAL", [], "Inlet air {value} C above the ASHRAE allowable 32 C"),
    ("ambient-temp-high", "ambient_temp_high", "ambient_temperature", ">", 27, 25, 3, 2,
     "WARNING", ["sensor"], "Ambient {value} C above {threshold} C"),
    ("humidity-high", "humidity_high", "relative_humidity", ">", 70, 65, 5, 3,
     "WARNING", ["sensor"], "Relative humidity {value}% above {threshold}%"),
    ("humidity-low", "humidity_low", "relative_humidity", "<", 20, 25, 5, 3,
     "WARNING", ["sensor"], "Relative humidity {value}% below {threshold}%"),
    ("power-draw-high", "power_draw_high", "power_draw", ">", 45000, 40000, 3, 2,
     "MAJOR", [], "Power draw {value} W above {threshold} W"),
]

# Rules with no metric: driven by endpoint communication state and by
# staleness, not by a threshold crossing.
STATE_RULES = [
    ("endpoint-unreachable", "endpoint_unreachable", "MAJOR",
     "No response from {device} over {protocol}"),
    ("telemetry-stale", "telemetry_stale", "WARNING",
     "Reachable but no telemetry for {stale_after_s}s"),
]

INSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, operator, threshold,
                            clear_threshold, dwell_samples, clear_dwell_samples,
                            severity, device_types, message_tpl, stale_after_s)
    VALUES (:name, :alarm_type, :metric_key, :operator, :threshold,
            :clear_threshold, :dwell_samples, :clear_dwell_samples,
            CAST(:severity AS severity_t), :device_types, :message_tpl, :stale_after_s)
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
        stale_after_s = EXCLUDED.stale_after_s
""")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(INSERT, [
        {"name": n, "alarm_type": at, "metric_key": m, "operator": o,
         "threshold": t, "clear_threshold": c, "dwell_samples": d,
         "clear_dwell_samples": cd, "severity": sev, "device_types": dt,
         "message_tpl": msg, "stale_after_s": None}
        for n, at, m, o, t, c, d, cd, sev, dt, msg in RULES
    ])
    conn.execute(INSERT, [
        {"name": n, "alarm_type": at, "metric_key": None, "operator": None,
         "threshold": None, "clear_threshold": None, "dwell_samples": 1,
         "clear_dwell_samples": 1, "severity": sev, "device_types": [],
         "message_tpl": msg, "stale_after_s": 600 if at == "telemetry_stale" else None}
        for n, at, sev, msg in STATE_RULES
    ])


def downgrade() -> None:
    names = [r[0] for r in RULES] + [r[0] for r in STATE_RULES]
    op.get_bind().execute(
        sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)"), {"names": names})
