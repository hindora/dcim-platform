"""A device is judged against its own normal (docs/35, Phase 3a).

The first L3 detectors: each learns, per series, what the device usually does
at each UTC hour of the day (analytics/baselines.fit_profile, nightly from
telemetry_1h) and finds the hours it does something else. They need no other
device's data, which is why they come first; the driver-based ones (power
given server load, battery temperature given the room) follow.

  SV-3  server CPU utilisation unlike its usual for the hour
  UP-3  UPS load unlike its usual for the hour (a lost feed, a transfer)
  PD-2  phase imbalance on a panel, switchgear or the utility feed above its usual

All advisory (panel-only, docs/35 section 7), all enabled - inert until a
series has `min_hours` of clean history over `min_days` days, which on the
part-time estate is weeks. Hour-of-DAY, not hour-of-week: most hour-of-week
slots never see data while the stack runs only on weekdays (docs/35 section 3).

`model_kind` gains 'profile_hod' for these fits.

Revision ID: 0125
Revises: 0124
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None

#: Common to the Phase 3a profiles. Tunable ones have a ParamSpec in
#: analytics/catalogue.py (`_l3`); the rest are fixed by docs/35.
_COMMON = {"v": 1, "min_hours": 120, "min_days": 10, "min_hour_samples": 8,
           "z_open": 4.0, "ewma_alpha": 0.3, "cusum_k": 0.5, "cusum_h": 5.0,
           "score_window_s": 1800, "coldstart_s": 1800, "min_samples": 3,
           "max_change_pct": 10.0, "exclude_cold_start": True,
           "exclude_maintenance": True, "dwell_s": 900, "clear_dwell_s": 1800,
           "reopen_window_s": 1800}


def _l3(did: str, types: list[str], metric: str, name: str, diagnosis: str,
        sigma_floor: float) -> dict:
    return {"id": did, "layer": 3, "phase": 3, "name": name, "diagnosis": diagnosis,
            "device_types": types, "primary_metric": metric, "required_metrics": [metric],
            "default_lane": "advisory", "default_kind": "undetermined",
            "alarm_type": None, "severity": None,
            "params": {**_COMMON, "sigma_floor": sigma_floor}, "enabled": True}


ROWS: list[dict] = [
    _l3("SV-3", ["server"], "cpu_utilization", "Server CPU unlike its usual",
        "CPU utilisation far from what this server usually runs at this hour: "
        "an unexpected workload, a runaway process, or a service that stopped", 2.0),
    _l3("UP-3", ["ups"], "load_pct", "UPS load unlike its usual",
        "UPS load far from its usual for the hour with no planned change: a lost "
        "downstream feed, a transfer to another source, or new load", 1.0),
    _l3("PD-2", ["mpp", "mcc", "switchgear", "utility_feed"], "phase_imbalance_pct",
        "Phase imbalance above its usual",
        "Phase imbalance higher than this panel usually carries at this hour: load "
        "moved onto one phase, or a failing conductor or connection", 0.5),
]

IDS = [r["id"] for r in ROWS]

_KINDS_OLD = ("ols", "profile_how", "curve", "first_order", "peer", "influence")
_KINDS_NEW = (*_KINDS_OLD, "profile_hod")


def _kind_check(kinds: tuple[str, ...]) -> str:
    return "model_kind = ANY (ARRAY[" + ", ".join(f"'{k}'::text" for k in kinds) + "])"


def _set_kinds(kinds: tuple[str, ...]) -> None:
    # Plain SQL: the naming convention would rewrite a name passed through
    # op.create_check_constraint, and this one must keep its exact name.
    op.execute("ALTER TABLE anomaly_baseline DROP CONSTRAINT ck_anomaly_baseline_model_kind")
    op.execute("ALTER TABLE anomaly_baseline ADD CONSTRAINT ck_anomaly_baseline_model_kind "
               f"CHECK ({_kind_check(kinds)})")


def upgrade() -> None:
    _set_kinds(_KINDS_NEW)
    stmt = sa.text("""
        INSERT INTO anomaly_detector (id, version, layer, phase, name, diagnosis,
                                      device_types, primary_metric, required_metrics,
                                      default_lane, default_kind, alarm_type, severity,
                                      cadence_s, params, enabled, updated_by)
        VALUES (:id, 1, :layer, :phase, :name, :diagnosis,
                CAST(:device_types AS text[]), :primary_metric,
                CAST(:required_metrics AS text[]), :default_lane, :default_kind,
                :alarm_type, CAST(:severity AS severity_t), 60,
                CAST(:params AS jsonb), :enabled, 'migration:0125')
        ON CONFLICT (id) DO NOTHING
    """)
    bind = op.get_bind()
    for row in ROWS:
        bind.execute(stmt, {**row, "params": json.dumps(row["params"])})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM anomaly_finding WHERE detector_id = ANY(:ids)"),
                 {"ids": IDS})
    bind.execute(sa.text("DELETE FROM anomaly_baseline WHERE model_kind = 'profile_hod'"))
    bind.execute(sa.text("DELETE FROM anomaly_detector WHERE id = ANY(:ids)"), {"ids": IDS})
    _set_kinds(_KINDS_OLD)
