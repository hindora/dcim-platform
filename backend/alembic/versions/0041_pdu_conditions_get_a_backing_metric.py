"""A lost clear left a PDU alarm standing for half an hour.

An IT alarm heals itself when the clear goes missing: `measured_clear` watches
the reading behind it and closes the row once that reading is back in the clear
band. A PDU alarm did not, and the reason was structural rather than deliberate.

The reconciler needs two things before it will look at a row -

    AND coalesce(r.metric_key, a.metric_key) IS NOT NULL
    AND (r.clear_threshold IS NOT NULL OR a.threshold IS NOT NULL)

- something to name the measurement, and something to name a limit. A
trap-raised PDU alarm has neither. Its threshold is NULL because no vendor puts
a limit in the notification (PowerNet carries the STATE it crossed, not the
number), and no rule carried a PDU trap's alarm_type, so the LEFT JOIN found
nothing. The row was filtered out before its metric was ever considered.

The effect, measured on this estate: every PDU alarm has closed either by the
device's own clear trap or by `reconciliation:aged` - never once by
`reconciliation:measured`. Aged-out closes on SILENCE. It cannot tell "the
condition recovered and the clear was lost" from "the condition is still live
and the strip stopped telling us", and it holds a false CRITICAL for the full
grace period either way.

These rules give the measured PDU conditions the same footing as cpu_high. The
alarm key is (device, alarm_type, instance), so a rule sharing the trap's
alarm_type maintains the SAME row rather than opening a second one - and the
thresholds are the bands the device plane itself uses, so the two detectors
agree instead of arguing.

There is no way to get metric-backed clearing without also letting the rule
raise: `measured_clear` joins `AND r.enabled`, and a disabled rule drives
nothing. That is the right trade anyway - it closes the mirror-image gap, where
a lost RAISE meant nobody noticed at all.

What is deliberately NOT here:

* breaker_tripped, outlet_off, outlet_failure, ground_fault, smoke_detected.
  State conditions with no continuous reading behind them. Nothing can close
  those on evidence, on real gear either - a clear trap or the timer is all
  there is.
* outlet_current_high. Its clear band is the strip's own breaker - 16 A, 30 A,
  32 A - and alarm_rule holds one threshold per alarm_type. Backing it with a
  single amp figure would clear a 32 A strip against a 16 A line. It is covered
  in percentage terms by pdu_load_high, which is SKU-independent.
* phase_imbalance. The plane does not publish an imbalance metric to watch.
"""

from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

PDU_TYPES = ["pdu", "floor_pdu"]

#: name, alarm_type, metric, operator, raise, clear, severity, message
#:
#: Raise and clear points are the device plane's own bands, so a rule and a trap
#: describing one condition cannot disagree about whether it is happening. A
#: clear point set tighter than the device's would close a row the strip still
#: considers alarmed, and the next re-assert would simply raise it again.
RULES = [
    ("pdu-load-high", "pdu_load_high", "load_pct", ">", 80, 70, "MAJOR",
     "PDU at {value}% of its nameplate, above {threshold}%"),
    ("pdu-load-critical", "pdu_load_critical", "load_pct", ">", 90, 85,
     "CRITICAL", "PDU at {value}% of its nameplate, above {threshold}%"),
    ("pdu-voltage-high", "voltage_high", "voltage_ln", ">", 240, 235, "MAJOR",
     "Input {value} V above {threshold} V"),
    ("pdu-voltage-low", "voltage_low", "voltage_ln", "<", 200, 205, "MAJOR",
     "Input {value} V below {threshold} V"),
    ("pdu-power-factor-low", "power_factor_low", "power_factor", "<", 0.70,
     0.75, "MAJOR", "Power factor {value}, below {threshold}"),
    ("pdu-intake-temp-high", "pdu_temp_high", "ambient_temperature", ">", 35,
     30, "MAJOR", "Intake air {value} C above {threshold} C"),
    ("pdu-intake-humidity-high", "pdu_humidity_high", "relative_humidity", ">",
     70, 60, "MAJOR", "Intake humidity {value}% above {threshold}%"),
]

UPSERT = sa.text("""
    INSERT INTO alarm_rule (name, alarm_type, metric_key, operator, threshold,
                            clear_threshold, dwell_samples, dwell_seconds,
                            clear_dwell_samples, severity, device_types,
                            message_tpl, enabled)
    VALUES (:name, :alarm_type, :metric_key, :operator, :threshold,
            :clear_threshold, 3, NULL, 2, CAST(:severity AS severity_t),
            :device_types, :message_tpl, true)
    ON CONFLICT (name) DO UPDATE SET
        alarm_type          = EXCLUDED.alarm_type,
        metric_key          = EXCLUDED.metric_key,
        operator            = EXCLUDED.operator,
        threshold           = EXCLUDED.threshold,
        clear_threshold     = EXCLUDED.clear_threshold,
        dwell_samples       = EXCLUDED.dwell_samples,
        clear_dwell_samples = EXCLUDED.clear_dwell_samples,
        severity            = EXCLUDED.severity,
        device_types        = EXCLUDED.device_types,
        message_tpl         = EXCLUDED.message_tpl,
        enabled             = EXCLUDED.enabled
""")


def upgrade() -> None:
    conn = op.get_bind()
    for name, alarm_type, metric, op_, raise_at, clear_at, sev, msg in RULES:
        conn.execute(UPSERT, {
            "name": name, "alarm_type": alarm_type, "metric_key": metric,
            "operator": op_, "threshold": raise_at, "clear_threshold": clear_at,
            "severity": sev, "device_types": PDU_TYPES, "message_tpl": msg,
        })


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DELETE FROM alarm_rule WHERE name = ANY(:names)"),
                 {"names": [r[0] for r in RULES]})
