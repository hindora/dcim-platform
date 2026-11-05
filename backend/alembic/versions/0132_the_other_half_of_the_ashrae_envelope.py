"""The other half of the ASHRAE envelope: dew point, and too cold.

The thermal guideline (ASHRAE TC 9.9) bounds the air at the server inlet on
four sides and this platform alarmed on one: too warm (27 C recommended,
32 C allowable). The rest:

- Moisture is bounded by DEW POINT, not relative humidity: the recommended
  envelope is -9 C to 15 C dew point, A1 allowable to 17 C. A hall at 24 C
  and 55 % RH is inside the RH limits and already at a 14.5 C dew point; the
  RH rules could not see it coming. Condensation on a chilled coil or a cold
  plate line is the consequence; ESD is the consequence at the dry end.
- Too cold is a fault too: 18 C recommended minimum, 15 C allowable for A1.
  Over-cooling wastes the plant and, below the dew point of warmer air, wets
  it. Nothing alarmed on it.

No probe in this estate reports a dew point, so the ingest worker derives
one (Magnus formula) wherever a device reports both temperature and humidity
in the same poll - a rack probe, a room sensor, a PDU with an environmental
probe. Any vendor's probe then answers the same question.

Revision ID: 0132
Revises: 0131
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None

# name, alarm_type, metric, op, threshold, clear, dwell, clear_dwell, severity,
# device_types, message
RULES = [
    ("dew-point-high", "dew_point_high", "dew_point", ">", 15, 14, 2, 3,
     "WARNING", ["sensor", "pdu"],
     "Dew point {value} C above the ASHRAE recommended 15 C: condensation risk on cold surfaces"),
    ("dew-point-critical", "dew_point_critical", "dew_point", ">", 17, 16, 2, 3,
     "CRITICAL", ["sensor", "pdu"],
     "Dew point {value} C above the ASHRAE A1 allowable 17 C"),
    ("dew-point-low", "dew_point_low", "dew_point", "<", -9, -7, 5, 3,
     "WARNING", ["sensor", "pdu"],
     "Dew point {value} C below the ASHRAE recommended -9 C: electrostatic discharge risk"),
    ("inlet-temp-low", "inlet_temp_low", "inlet_temperature", "<", 18, 19, 5, 3,
     "WARNING", [],
     "Inlet air {value} C below the ASHRAE recommended 18 C: over-cooled"),
    ("ambient-temp-low", "ambient_temp_low", "ambient_temperature", "<", 18, 19, 5, 3,
     "WARNING", ["sensor"],
     "Ambient {value} C below the ASHRAE recommended 18 C: over-cooled"),
]

INSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, operator, threshold,
                            clear_threshold, dwell_samples, clear_dwell_samples,
                            severity, device_types, message_tpl, stale_after_s)
    VALUES (:name, :alarm_type, :metric_key, :operator, :threshold,
            :clear_threshold, :dwell_samples, :clear_dwell_samples,
            CAST(:severity AS severity_t), :device_types, :message_tpl, NULL)
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
        message_tpl = EXCLUDED.message_tpl
""")


def upgrade() -> None:
    op.get_bind().execute(INSERT, [
        {"name": n, "alarm_type": at, "metric_key": m, "operator": o,
         "threshold": t, "clear_threshold": c, "dwell_samples": d,
         "clear_dwell_samples": cd, "severity": sev, "device_types": dt,
         "message_tpl": msg}
        for n, at, m, o, t, c, d, cd, sev, dt, msg in RULES
    ])


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)"),
                          {"names": [r[0] for r in RULES]})
