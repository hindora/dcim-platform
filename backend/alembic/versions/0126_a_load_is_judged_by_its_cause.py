"""A reading is judged by what drives it (docs/35, Phase 3a part 2).

  SV-4  server power unlike its workload: a regression of power_draw on the
        server's own cpu_utilization. "8 kW to 12 kW with no more work" at a
        rack is the sum of these; judged per server it also names the one that
        changed (a failing supply, a lifted power cap, added hardware).
        Per server, not per PDU: judged where the cause is, it names the box
        that changed. (A PDU's total also barely varies - a few percent over a
        day - so a PDU-level regression has little signal to learn from. It
        does meter the sum of what is plugged into it, within its noise and the
        poll timing of its loads; an early check that compared it with its
        SERVERS alone, leaving out its switches and CDUs, suggested otherwise.)
  TH-1  rack intake unusually far above its room's supply air: an hour-of-day
        profile of (inlet - mean room CRAH supply). The gap, not the inlet: a
        setpoint raised by 2 K moves both and the gap stays; hot air
        recirculating into one rack widens only its gap.

Both advisory, both enabled, both inert until learned (docs/35 section 6).

Revision ID: 0126
Revises: 0125
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_COMMON = {"v": 1, "min_hours": 120, "min_days": 10, "min_hour_samples": 8,
           "z_open": 4.0, "ewma_alpha": 0.3, "cusum_k": 0.5, "cusum_h": 5.0,
           "score_window_s": 1800, "coldstart_s": 1800, "min_samples": 3,
           "max_change_pct": 10.0, "exclude_cold_start": True,
           "exclude_maintenance": True, "dwell_s": 900, "clear_dwell_s": 1800,
           "reopen_window_s": 1800}

ROWS: list[dict] = [
    {"id": "SV-4", "layer": 3, "phase": 3, "name": "Server power unlike its workload",
     "diagnosis": "Power draw far from what this server's own CPU load explains: a "
                  "failing power supply, a changed power cap, or hardware added or "
                  "removed",
     "device_types": ["server"], "primary_metric": "power_draw",
     "required_metrics": ["power_draw", "cpu_utilization"],
     "default_lane": "advisory", "default_kind": "undetermined", "alarm_type": None,
     "severity": None, "enabled": True,
     "params": {**_COMMON, "sigma_floor": 5.0, "min_r2": 0.3, "x_margin": 0.1}},
    {"id": "TH-1", "layer": 3, "phase": 3, "name": "Rack intake above its room's supply",
     "diagnosis": "This rack's intake is further above the room's supply air than it "
                  "usually is: hot exhaust recirculating into it, a missing blanking "
                  "panel, or a blocked or misplaced floor tile",
     "device_types": ["server"], "primary_metric": "inlet_temperature",
     "required_metrics": ["inlet_temperature"],
     "default_lane": "advisory", "default_kind": "undetermined", "alarm_type": None,
     "severity": None, "enabled": True,
     "params": {**_COMMON, "sigma_floor": 0.3}},
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
                CAST(:params AS jsonb), :enabled, 'migration:0126')
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
