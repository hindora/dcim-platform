"""The rules that need no history.

Phase 1 of the anomaly detector (docs/28, docs/29 section 12.1, docs/33):
the detectors that can judge a device from what it is reporting now, with no
fitted baseline behind them.

  L0  DQ-1 .. DQ-5   data quality: dropout, a non-GOOD run, impossible values
                     and sentinels, spikes, a flatline while a coupled
                     variable moves. Advisory findings of kind `sensor`; they
                     also MASK a series so no L2 rule reasons from bad data.
  L2  18 rows        ASHRAE Guideline 36-style state-gated rules: required
                     points, an operating-state gate, a tolerance and a set /
                     clear dwell. Raised as `detection = derived` alerts,
                     capped at MINOR by the sweep (docs/33 section 1 item 2).
                     CT-2, CT-3, AT-1 and PL-1 are seeded DISABLED: the
                     campaign runner enables each for its own run.
  L5  the switch row The correlator's on/off and its `major_enabled` switch,
                     so every Phase 2 rollback is one PATCH. Seeded here so
                     Phase 2 needs no migration.

Taxonomy is code, not rows (docs/29 D5): the alarm types are in
app/core/alert_taxonomy.py BY_ALARM_TYPE, and nothing here writes SQL for it.

`ON CONFLICT (id) DO NOTHING`: re-running this after an operator PATCH leaves
the operator's params intact.

Params are flat (one level), unlike the nested `gate` of docs/29 section 7.1:
a PATCH is a partial shallow merge, and a nested object would be replaced
whole by a one-key edit (analytics/catalogue.py explains). CD-1's margin is
2.0 K, the display verdict's CDU_SUPPLY_FAULT_MARGIN_K (docs/29 D18).

Downgrade DELETES every finding these detectors raised - the FK from
anomaly_finding is RESTRICT, so it has to - and then the rows; overrides,
state, baselines and detector-scoped exclusions cascade. That data loss is
deliberate. Derived alarms already raised stay in `alarm` as history.

Revision ID: 0112
Revises: 0111
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

_L0_TYPES = ["crah", "chiller", "cooling_tower", "pump", "cdu", "valve", "sensor", "pdu",
             "ups", "transformer", "generator", "server"]

#: Common to every L0/L2 row (docs/29 section 7.1).
_COMMON = {"v": 1, "reopen_window_s": 1800, "min_samples": 3, "window_s": 1800}


def _l0(did: str, name: str, diagnosis: str, params: dict) -> dict:
    return {"id": did, "layer": 0, "phase": 1, "name": name, "diagnosis": diagnosis,
            "device_types": _L0_TYPES, "primary_metric": "*", "required_metrics": [],
            "default_lane": "advisory", "default_kind": "sensor", "alarm_type": None,
            "severity": None, "params": {**_COMMON, **params}, "enabled": True}


def _l2(did: str, types: list[str], primary: str, required: list[str], alarm_type: str,
        severity: str, name: str, diagnosis: str, params: dict,
        enabled: bool = True) -> dict:
    return {"id": did, "layer": 2, "phase": 1, "name": name, "diagnosis": diagnosis,
            "device_types": types, "primary_metric": primary,
            "required_metrics": required, "default_lane": "alert",
            "default_kind": "process", "alarm_type": alarm_type, "severity": severity,
            "params": {**_COMMON, **params}, "enabled": enabled}


_GATE_AIR = {"exclude_fire_predischarge": True, "exclude_maintenance": True}
_MAINT = {"exclude_maintenance": True}
_HUNT = {"min_reversals": 6, "min_step_pct": 5.0, "window_s": 1800, "dwell_s": 0,
         "clear_dwell_s": 1800}

ROWS: list[dict] = [
    # --- L0 ----------------------------------------------------------------
    _l0("DQ-1", "Series dropout",
        "One series stopped arriving while its endpoint still delivers the others",
        {"grace_intervals": 3, "min_grace_s": 300, "max_grace_s": 10_800,
         "clear_samples": 2}),
    _l0("DQ-2", "Non-GOOD run",
        "Most recent samples arrived flagged suspect or bad by the device or ingest",
        {"window_s": 1800, "min_bad_fraction": 0.5, "min_samples": 5, "dwell_s": 0,
         "clear_dwell_s": 300}),
    _l0("DQ-3", "Impossible value",
        "Reading outside the physical range, or a known failure sentinel",
        {"bounds": {"supply_air_temp": [5, 40], "return_air_temp": [10, 55],
                    "inlet_temperature": [5, 50], "ambient_temperature": [0, 50],
                    "water_supply_temp": [1, 60], "relative_humidity": [0.5, 99.5]},
         "sentinels": [-40.0, -127.0, 85.0, 327.67, -327.68],
         "dwell_samples": 3, "clear_samples": 3}),
    _l0("DQ-4", "Spiky sensor",
        "Single-sample spikes, more often than a healthy sensor produces them",
        {"window": 11, "k_mad": 5.0, "min_mad_abs": {"temperature": 0.1, "pct": 0.5},
         "max_spikes_per_h": 6, "dwell_s": 0, "clear_dwell_s": 3600}),
    _l0("DQ-5", "Flatline",
        "The reading has not moved while the variable it follows has",
        {"min_samples": 20, "clear_samples": 2,
         "coupled": {"supply_air_temp": [["valve_position_pct", "CHW"],
                                         ["fan_speed_pct", ""]],
                     "water_supply_temp": [["compressor_load_pct", ""]],
                     "inlet_temperature": [["cpu_utilization", ""]]},
         "coupled_min_range": {"pct": 5.0, "temperature": 0.5}}),
    # --- L2 ----------------------------------------------------------------
    _l2("CR-1", ["crah"], "supply_air_temp",
        ["supply_air_temp", "air_setpoint_temp", "valve_position_pct{CHW}",
         "equipment_state{Unit_Running}"],
        "crah_supply_over_setpoint", "MINOR", "CRAH supply over setpoint",
        "Discharge held above setpoint: chilled-water starved if the valve is open, "
        "otherwise a control fault",
        {"margin_k": 3.0, "clear_margin_k": 2.0, "valve_starved_pct": 95.0,
         "running_min_s": 600, **_GATE_AIR, "dwell_s": 900, "clear_dwell_s": 300}),
    _l2("CR-2", ["crah"], "return_air_temp",
        ["return_air_temp", "supply_air_temp", "fan_speed_pct",
         "equipment_state{Unit_Running}"],
        "crah_low_delta_t", "MINOR", "CRAH low delta-T",
        "Return barely warmer than supply: bypass air or an over-provisioned unit",
        {"min_delta_k": 4.0, "clear_delta_k": 5.0, "min_fan_pct": 30.0,
         "running_min_s": 600, **_GATE_AIR, "dwell_s": 900, "clear_dwell_s": 600}),
    _l2("CR-6", ["crah"], "valve_position_pct",
        ["valve_position_pct{CHW}", "equipment_state{Unit_Running}"],
        "crah_valve_hunting", "WARNING", "CRAH valve hunting",
        "The chilled-water valve keeps reversing: loop tuning or a sticking actuator",
        {**_HUNT, "running_min_s": 600, **_GATE_AIR}),
    _l2("CH-1", ["chiller"], "water_supply_temp",
        ["water_supply_temp{CHW}", "water_setpoint_temp{CHW}", "compressor_load_pct",
         "equipment_state{Chiller_Running}"],
        "chiller_chw_over_setpoint", "MINOR", "Chiller CHW over setpoint",
        "Leaving chilled water above setpoint with capacity in hand",
        {"margin_k": 1.0, "clear_margin_k": 0.5, "max_load_pct": 95.0,
         "running_min_s": 900, "restart_exclusion_s": 480, **_MAINT,
         "dwell_s": 900, "clear_dwell_s": 300}),
    _l2("CH-4", ["chiller"], "equipment_state", ["equipment_state{Chiller_Running}"],
        "chiller_short_cycling", "MINOR", "Chiller short cycling",
        "More starts in an hour than a compressor should see",
        {"max_starts": 3, "window_s": 3600, "lookback_s": 1800, "dwell_s": 0,
         "clear_dwell_s": 3600}),
    _l2("CH-5", ["chiller"], "water_return_temp",
        ["water_return_temp{COND}", "water_pressure{COND}",
         "equipment_state{Chiller_Running}"],
        "chiller_condenser_rising", "MINOR", "Chiller condenser rising",
        "Condenser water climbing toward the trip point: heat rejection is failing",
        {"rise_k_per_min": 0.3, "trip_c": 42.0, "horizon_s": 600, **_MAINT,
         "dwell_samples": 2, "clear_dwell_s": 600}),
    _l2("PL-1", ["chiller"], "water_return_temp",
        ["water_return_temp{CHW}", "water_supply_temp{CHW}", "water_flow{CHW}"],
        "chiller_low_delta_t", "MINOR", "Chiller low delta-T",
        "Chilled-water range collapsing while flow rises: low delta-T syndrome",
        {"design_delta_k": 5.5, "min_fraction": 0.6, "flow_rising": True, **_MAINT,
         "dwell_s": 1800, "clear_dwell_s": 900}, enabled=False),
    _l2("CD-1", ["cdu"], "water_supply_temp",
        ["water_supply_temp{}", "valve_position_pct{FACILITY_CHW}", "water_setpoint_temp",
         "equipment_state{Unit_Running}"],
        "cdu_facility_starved", "MINOR", "CDU facility water starved",
        "Facility valve wide open and the secondary loop still over setpoint",
        {"valve_pct": 95.0, "over_setpoint_k": 2.0, **_MAINT, "dwell_s": 300,
         "clear_dwell_s": 300}),
    _l2("CD-6", ["cdu"], "water_supply_temp",
        ["water_supply_temp{}", "equipment_state{Unit_Running}"],
        "cdu_condensation_risk", "MINOR", "CDU condensation risk",
        "Secondary supply close to the room's dew point: condensation on the plates",
        {"min_margin_k": 2.0, "clear_margin_k": 3.0, "dew_source": "room_max", **_MAINT,
         "dwell_s": 600, "clear_dwell_s": 600}),
    _l2("VA-1", ["valve"], "valve_position_pct",
        ["valve_position_pct{MEASURED}", "valve_position_pct{COMMANDED}"],
        "valve_position_mismatch", "MINOR", "Valve position mismatch",
        "The valve is not where it was told to be: stuck stem, slipped linkage, dead "
        "actuator, or a lying feedback",
        {"deviation_pct": 10.0, "clear_pct": 5.0, "settle_s": 120, **_MAINT,
         "dwell_s": 600, "clear_dwell_s": 300}),
    _l2("VA-2", ["valve"], "valve_position_pct", ["valve_position_pct{MEASURED}"],
        "valve_hunting", "WARNING", "Valve hunting",
        "The valve keeps reversing: loop tuning or a sticking actuator",
        {**_HUNT, **_MAINT}),
    _l2("PU-3", ["pump"], "water_flow",
        ["water_flow", "equipment_state{Run_Status}", "pump_speed_pct"],
        "pump_failed_to_prove", "MINOR", "Pump failed to prove",
        "The pump's run proof disagrees with its speed and flow",
        {"min_speed_pct": 10.0, "min_flow_fraction": 0.05, "prove_s": 60, **_MAINT,
         "dwell_samples": 2, "clear_samples": 3}),
    _l2("RM-2", ["sensor", "pdu"], "*", [],
        "inlet_rate_of_change", "MINOR", "Inlet rate of change",
        "Intake temperature moving faster than ASHRAE's 20 K/h",
        {"max_rate_k_per_h": 20.0, "window_s": 900, **_GATE_AIR, "dwell_samples": 3,
         "clear_dwell_s": 900}),
    _l2("RM-3", ["sensor", "pdu"], "inlet_temperature", ["inlet_temperature"],
        "rack_hot_spot", "MINOR", "Rack hot spot",
        "This rack's intake well above the rest of the room",
        {"margin_k": 3.0, "clear_margin_k": 2.0, "min_room_sensors": 5, **_GATE_AIR,
         "dwell_s": 900, "clear_dwell_s": 600}),
    _l2("GN-1", ["generator"], "fuel_level_pct",
        ["fuel_level_pct", "equipment_state{Engine_Running}"],
        "generator_fuel_loss", "MINOR", "Generator fuel loss",
        "Fuel level falling while the engine is stopped: a leak or theft",
        {"min_drop_pct": 1.0, "window_s": 3600, "not_running_min_s": 1800,
         "ignore_rises": True, "dwell_s": 1800, "clear_dwell_s": 1800}),
    _l2("CT-2", ["cooling_tower"], "fan_speed_pct",
        ["fan_speed_pct", "water_supply_temp{COND}", "outdoor_wet_bulb_temp",
         "equipment_state{Fan_Status}"],
        "tower_fan_saturated", "MINOR", "Tower fan saturated",
        "Fan at full speed and the approach still climbing: the cell is out of capacity",
        {"fan_pct": 98.0, "approach_rise_k_per_h": 0.5, **_MAINT, "dwell_s": 1200,
         "clear_dwell_s": 600}, enabled=False),
    _l2("CT-3", ["cooling_tower"], "basin_level_pct",
        ["basin_level_pct", "makeup_water_flow"],
        "tower_basin_imbalance", "MINOR", "Tower basin imbalance",
        "Basin falling while make-up runs: a leak, an overflow or a stuck drain",
        {"fall_pct_per_h": 2.0, **_MAINT, "dwell_s": 1800, "clear_dwell_s": 1800},
        enabled=False),
    _l2("AT-1", ["ats"], "transfer_count", ["transfer_count"],
        "ats_transfer_rate", "MINOR", "ATS transfer rate",
        "More transfers in a day than a healthy utility feed causes",
        {"max_transfers_24h": 2, "dwell_s": 0, "clear_dwell_s": 86_400}, enabled=False),
    # --- the L5 switch row (docs/29 D17) -----------------------------------
    {"id": "L5", "layer": 5, "phase": 2, "name": "Cooling correlator",
     "diagnosis": "Groups findings and cooling alarms under one ranked root",
     "device_types": [], "primary_metric": "*", "required_metrics": [],
     "default_lane": "advisory", "default_kind": "process", "alarm_type": None,
     "severity": None, "enabled": True,
     "params": {"v": 1, "window_s": 600, "hold_down_s": 900, "major_enabled": False,
                "major_min_racks": 3, "use_influence": False}},
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
                CAST(:params AS jsonb), :enabled, 'migration:0112')
        ON CONFLICT (id) DO NOTHING
    """)
    bind = op.get_bind()
    for row in ROWS:
        bind.execute(stmt, {**row, "params": json.dumps(row["params"])})


def downgrade() -> None:
    bind = op.get_bind()
    # RESTRICT on anomaly_finding.detector_id: the findings go first, on purpose.
    bind.execute(sa.text("DELETE FROM anomaly_finding WHERE detector_id = ANY(:ids)"),
                 {"ids": IDS})
    bind.execute(sa.text("DELETE FROM anomaly_detector WHERE id = ANY(:ids)"), {"ids": IDS})
