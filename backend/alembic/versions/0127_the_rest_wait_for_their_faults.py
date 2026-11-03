"""The rest of Phase 3a, seeded off until the simulator can prove them (docs/35 §5).

  UP-1  UPS battery temperature above its room's air, against that gap's usual
        (a gap profile: what warms the whole room is not the battery's fault)
  UP-2  UPS battery runtime given its load, as a straight line on 100/load -
        runtime falls as the inverse of load - so a fading battery shows as
        less runtime at the same load
  SV-1  server CPU temperature given its CPU load and intake (heatsink, paste)
  SV-2  each server fan given intake and CPU load (wear, a blocked intake)
  NW-1  an interface's receive-error rate above its usual for the hour

Off, because the simulator has no fault that moves them yet (battery ageing,
fan wear, server thermal, error injection): an unproven detector that is on is
how an advisory queue earns a reputation for noise. Turning one on needs no
waiting - the nightly fit reads the last 30 days of hourly history, so it
learns at once.

Revision ID: 0127
Revises: 0126
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

_COMMON = {"v": 1, "min_hours": 120, "min_days": 10, "min_hour_samples": 8,
           "z_open": 4.0, "ewma_alpha": 0.3, "cusum_k": 0.5, "cusum_h": 5.0,
           "score_window_s": 1800, "coldstart_s": 1800, "min_samples": 3,
           "max_change_pct": 10.0, "exclude_cold_start": True,
           "exclude_maintenance": True, "dwell_s": 900, "clear_dwell_s": 1800,
           "reopen_window_s": 1800}
_OLS = {"min_r2": 0.3, "x_margin": 0.1}


def _row(did, types, metric, required, name, diagnosis, extra):
    return {"id": did, "layer": 3, "phase": 3, "name": name, "diagnosis": diagnosis,
            "device_types": types, "primary_metric": metric, "required_metrics": required,
            "default_lane": "advisory", "default_kind": "undetermined", "alarm_type": None,
            "severity": None, "enabled": False, "params": {**_COMMON, **extra}}


ROWS: list[dict] = [
    _row("UP-1", ["ups"], "battery_temperature", ["battery_temperature"],
         "UPS battery warmer than its room",
         "The battery is further above the room's air than it usually runs: rising "
         "internal resistance from ageing, a high float charge, or a cell failing",
         {"sigma_floor": 0.3}),
    _row("UP-2", ["ups"], "battery_runtime", ["battery_runtime", "load_pct"],
         "UPS runtime short for its load",
         "Less battery runtime than this UPS gives at the same load: battery capacity "
         "fading", {**_OLS, "sigma_floor": 0.5}),
    _row("SV-1", ["server"], "cpu_temperature",
         ["cpu_temperature", "cpu_utilization", "inlet_temperature"],
         "Server CPU hotter than its load explains",
         "CPU temperature above what this server's load and intake explain: a "
         "heatsink or thermal paste problem, or airflow blocked inside the chassis",
         {**_OLS, "sigma_floor": 1.0}),
    _row("SV-2", ["server"], "fan_speed",
         ["fan_speed", "inlet_temperature", "cpu_utilization"],
         "Server fan faster than its conditions explain",
         "A fan spinning faster than this server's intake and load call for: a worn "
         "bearing, a blocked intake, or a neighbouring fan failing",
         {**_OLS, "sigma_floor": 100.0}),
    _row("NW-1", ["switch", "router"], "if_in_error_rate", ["if_in_error_rate"],
         "Interface errors above their usual",
         "Receive errors on this interface above its usual rate: a failing optic, a "
         "dirty connector, a damaged cable, or a duplex mismatch",
         {"sigma_floor": 0.1}),
]

IDS = [r["id"] for r in ROWS]


def upgrade() -> None:
    stmt = sa.text("""
        INSERT INTO anomaly_detector (id, version, layer, phase, name, diagnosis,
                                      device_types, primary_metric, required_metrics,
                                      default_lane, default_kind, alarm_type, severity,
                                      cadence_s, params, enabled, updated_by)
        VALUES (:id, 1, :layer, :phase, :name, :diagnosis,
                CAST(:device_types AS text[]), :primary_metric,
                CAST(:required_metrics AS text[]), :default_lane, :default_kind,
                :alarm_type, CAST(:severity AS severity_t), 60,
                CAST(:params AS jsonb), :enabled, 'migration:0127')
        ON CONFLICT (id) DO NOTHING
    """)
    bind = op.get_bind()
    for row in ROWS:
        bind.execute(stmt, {**row, "params": json.dumps(row["params"])})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM anomaly_finding WHERE detector_id = ANY(:ids)"),
                 {"ids": IDS})
    bind.execute(sa.text("DELETE FROM anomaly_baseline WHERE detector_id = ANY(:ids)"),
                 {"ids": IDS})
    bind.execute(sa.text("DELETE FROM anomaly_detector WHERE id = ANY(:ids)"), {"ids": IDS})
