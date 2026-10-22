"""The detector catalogue: what each detector is, and what it is on this device.

`anomaly_detector` holds about sixty rows and is read whole (docs/29 section
4.1); `anomaly_override` holds the per-device exceptions. This module turns
the two into the one question the sweep asks per (detector, device): is it
on here, and with which parameters.

Three rules live here and nowhere else:

* **Override merge.** Effective params are a shallow merge, detector.params
  || override.params, the override winning key by key. Effective enabled is
  DCIM_ANOMALY_ENABLED AND detector.enabled AND COALESCE(override.enabled,
  true), ignoring an override whose expires_at has passed (docs/29 4.2).
  Nested objects (`gate`) are replaced whole, not merged: that is what
  Postgres `||` does, and the API documents the same.

* **Missing-point disable.** ASHRAE Guideline 36 disables any alarm or
  sequence whose required points are not all present and reliable, rather
  than letting it run on a partial picture. A detector whose
  required_metrics are not all mapped on a device is OFF on that device,
  with the missing points named so the health view can say why.

* **The severity cap.** Derived alerts are capped at MINOR until the Phase 2
  gate: under the default integration policy a MAJOR alarm opens a ticket
  after 300 s, and a MAJOR derived alarm raised before the L5 correlator
  exists would ticket every symptom (docs/33 section 1, item 2).

Loading from the database is in repositories/anomalies.py; this module is
pure so the rules are unit-testable without one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: The ceiling on any severity the sweep raises with source="analytics".
#: Only an L5 incident root may exceed it, and only behind the L5 row's
#: `major_enabled` switch (docs/30 section 9).
DERIVED_SEVERITY_CAP = "MINOR"

#: The correlator's switch row (docs/29 4.1). It scores nothing.
L5_ID = "L5"

# --- shared constants: the display verdict and the alert use ONE value --------
#
# Doc 28 section "code": a CRAH the room page calls `high_supply` and a CR-1
# alert that disagrees about the same unit would be two answers to one
# question. These live here and services/thermal.py and services/plant.py
# import them back, so the display and the detector cannot drift
# (tests/test_anomaly_rule_parity.py).

#: CR-1 / classify_crah: a CRAH discharging this far over its setpoint has
#: stopped cooling (services/thermal.py SUPPLY_FAULT_MARGIN_K).
CRAH_SUPPLY_FAULT_MARGIN_K = 3.0
#: RM-3 / hot_spots: a rack this far above the room's p90 inlet is a hot spot.
HOT_SPOT_MARGIN_K = 3.0
#: CD-1 / classify_cdu: secondary supply this far over setpoint (docs/29 D18).
CDU_SUPPLY_FAULT_MARGIN_K = 2.0
#: VA-1 / plant verdict: commanded vs measured gap that means the actuator is
#: not obeying (services/plant.py VALVE_DEVIATION_PCT).
VALVE_DEVIATION_PCT = 10.0
#: RM-2: ASHRAE's 20 K/h maximum rate of change (core/ashrae.py Envelope).
MAX_RATE_K_PER_H = 20.0

_SEVERITY_RANK = {
    "CLEAR": 0, "INFO": 1, "WARNING": 2, "MINOR": 3, "MAJOR": 4, "CRITICAL": 5,
}

#: 'key' or 'key{INSTANCE}', as anomaly_detector.required_metrics stores them.
_REQUIRED_RE = re.compile(r"^(?P<key>[a-z][a-z0-9_]*)(?:\{(?P<inst>[^{}]*)\})?$")


def cap_severity(severity: str, cap: str = DERIVED_SEVERITY_CAP) -> str:
    """min(severity, cap) on the alarm severity ladder.

    An unknown severity is treated as above the cap and capped: failing safe
    is failing quiet here, because the expensive mistake is a ticket.
    """
    s = (severity or "").upper()
    if _SEVERITY_RANK.get(s, 99) > _SEVERITY_RANK[cap]:
        return cap
    return s


@dataclass(frozen=True)
class Detector:
    id: str
    version: int
    layer: int
    phase: int
    name: str
    diagnosis: str
    device_types: tuple[str, ...]
    primary_metric: str
    required_metrics: tuple[str, ...] = ()
    default_lane: str = "advisory"
    default_kind: str = "undetermined"
    alarm_type: str | None = None
    severity: str | None = None
    cadence_s: int = 60
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Detector:
        return cls(
            id=row["id"], version=int(row["version"]), layer=int(row["layer"]),
            phase=int(row["phase"]), name=row["name"], diagnosis=row["diagnosis"],
            device_types=tuple(row.get("device_types") or ()),
            primary_metric=row["primary_metric"],
            required_metrics=tuple(row.get("required_metrics") or ()),
            default_lane=row.get("default_lane") or "advisory",
            default_kind=row.get("default_kind") or "undetermined",
            alarm_type=row.get("alarm_type"), severity=row.get("severity"),
            cadence_s=int(row.get("cadence_s") or 60),
            params=dict(row.get("params") or {}),
            enabled=bool(row.get("enabled", True)))


@dataclass(frozen=True)
class Override:
    detector_id: str
    device_id: str
    params: Mapping[str, Any] | None = None
    enabled: bool | None = None
    expires_at: datetime | None = None
    reason: str | None = None

    def active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Override:
        return cls(
            detector_id=row["detector_id"], device_id=str(row["device_id"]),
            params=row.get("params"), enabled=row.get("enabled"),
            expires_at=row.get("expires_at"), reason=row.get("reason"))


def parse_required(spec: str) -> tuple[str, str | None]:
    """'water_flow' -> ('water_flow', None); 'valve_position_pct{CHW}' ->
    ('valve_position_pct', 'CHW'). None instance means any instance."""
    m = _REQUIRED_RE.match(spec)
    if not m:
        raise ValueError(f"bad required_metrics entry {spec!r}")
    return m.group("key"), m.group("inst")


def missing_points(required: Iterable[str],
                   available: Iterable[tuple[str, str]]) -> list[str]:
    """The required_metrics entries a device cannot supply.

    `available` is the device's (metric_key, instance) pairs. A bare key is
    satisfied by any instance of it; 'key{INST}' needs that instance exactly.
    """
    have = set(available)
    keys = {k for k, _ in have}
    out = []
    for spec in required:
        key, inst = parse_required(spec)
        ok = key in keys if inst is None else (key, inst) in have
        if not ok:
            out.append(spec)
    return out


@dataclass(frozen=True)
class Effective:
    """What a detector is on one device after the override and the G36 check."""

    detector: Detector
    params: Mapping[str, Any]
    enabled: bool
    #: Why it is off, when it is: 'feature_off' | 'detector_disabled' |
    #: 'muted' | 'wrong_device_type' | 'missing_points' | 'switch_row'.
    reason: str | None = None
    missing: tuple[str, ...] = ()


def merge_params(base: Mapping[str, Any],
                 override: Mapping[str, Any] | None) -> dict[str, Any]:
    """Shallow merge, override winning key by key (Postgres `jsonb ||`)."""
    out = dict(base)
    if override:
        out.update(override)
    return out


def effective(detector: Detector, *, feature_enabled: bool, device_type: str | None,
              override: Override | None = None, now: datetime | None = None,
              available: Iterable[tuple[str, str]] | None = None) -> Effective:
    """Resolve one detector on one device.

    `available` None skips the missing-point check (the caller has not loaded
    the device's points); an empty iterable means the device has none.
    """
    ov = override if (override is not None and (now is None or override.active(now))) else None
    params = merge_params(detector.params, ov.params if ov else None)

    def off(reason: str, missing: tuple[str, ...] = ()) -> Effective:
        return Effective(detector, params, False, reason, missing)

    if detector.id == L5_ID or detector.layer == 5:
        return off("switch_row")
    if not feature_enabled:
        return off("feature_off")
    if not detector.enabled:
        return off("detector_disabled")
    if ov is not None and ov.enabled is False:
        return off("muted")
    if device_type is not None and device_type not in detector.device_types:
        return off("wrong_device_type")
    if available is not None:
        gone = missing_points(detector.required_metrics, available)
        if gone:
            return off("missing_points", tuple(gone))
    return Effective(detector, params, True)


class Catalogue:
    """The detector rows and overrides, as the sweep holds them between
    refreshes. Built from repositories/anomalies.load_catalogue."""

    def __init__(self, detectors: Iterable[Detector],
                 overrides: Iterable[Override] = ()) -> None:
        self.detectors: dict[str, Detector] = {d.id: d for d in detectors}
        self.overrides: dict[tuple[str, str], Override] = {
            (o.detector_id, o.device_id): o for o in overrides}

    def __len__(self) -> int:
        return len(self.detectors)

    def get(self, detector_id: str) -> Detector | None:
        return self.detectors.get(detector_id)

    def scorers(self) -> list[Detector]:
        """Every row except the L5 switch, in id order."""
        return [d for k, d in sorted(self.detectors.items()) if d.layer != 5]

    def for_device_type(self, device_type: str) -> list[Detector]:
        return [d for d in self.scorers() if device_type in d.device_types]

    def l5_major_enabled(self) -> bool:
        row = self.detectors.get(L5_ID)
        return bool(row and row.enabled and row.params.get("major_enabled", False))

    def resolve(self, detector_id: str, device_id: str, *, feature_enabled: bool,
                device_type: str | None, now: datetime | None = None,
                available: Iterable[tuple[str, str]] | None = None) -> Effective | None:
        d = self.detectors.get(detector_id)
        if d is None:
            return None
        return effective(d, feature_enabled=feature_enabled, device_type=device_type,
                         override=self.overrides.get((detector_id, device_id)),
                         now=now, available=available)


# --- param_schema: the only validation for PATCH and override params ----------
#
# docs/30 section 6.1 (doc 31 D9). One ParamSpec per tunable key; a key in a
# row's params with no spec renders read-only and is refused by PATCH
# (`unknown_param`). `default` is the 0112 seed value (docs/29 section 12.1, as
# corrected by docs/33 section 3.7), checked against the migration by
# tests/test_anomaly_catalogue.py.
#
# Params are FLAT (docs/29 section 7.1 nests the gate keys under `gate`). A
# PATCH is a partial, shallow merge, and the override merge is Postgres `||`:
# a nested `gate` would be replaced whole by a one-key edit, silently dropping
# its siblings. Flat keys make "change one number" mean exactly that.


@dataclass(frozen=True)
class ParamSpec:
    key: str
    label: str
    kind: str                     # 'number' | 'int' | 'bool' | 'enum'
    default: Any
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: tuple[str, ...] | None = None
    help: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"key": self.key, "label": self.label, "kind": self.kind,
                               "default": self.default}
        for k in ("unit", "min", "max", "step", "help"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.options is not None:
            out["options"] = list(self.options)
        return out


def _dwell(default: int) -> ParamSpec:
    return ParamSpec("dwell_s", "Set dwell", "int", default, "s", 0, 86_400, 60,
                     help="Condition must hold this long before a finding opens")


def _clear(default: int) -> ParamSpec:
    return ParamSpec("clear_dwell_s", "Clear dwell", "int", default, "s", 0, 86_400, 60,
                     help="And be false this long before it resolves")


def _samples(key: str, label: str, default: int, hi: int = 50) -> ParamSpec:
    return ParamSpec(key, label, "int", default, "samples", 1, hi, 1)


_REOPEN = ParamSpec("reopen_window_s", "Reopen window", "int", 1800, "s", 0, 604_800, 60,
                    help="A re-raise inside this reopens the same finding")
_RUN = ParamSpec("running_min_s", "Running at least", "int", 600, "s", 0, 7200, 60)
_FIRE = ParamSpec("exclude_fire_predischarge", "Ignore during gas pre-discharge", "bool",
                  True)
_MAINT = ParamSpec("exclude_maintenance", "Ignore under a maintenance window", "bool", True)


def _hunting() -> list[ParamSpec]:
    return [
        ParamSpec("min_reversals", "Reversals", "int", 6, None, 2, 50, 1,
                  help="Direction changes inside the window that count as hunting"),
        ParamSpec("min_step_pct", "Smallest move", "number", 5.0, "%", 0.5, 50, 0.5),
        ParamSpec("window_s", "Window", "int", 1800, "s", 300, 7200, 60),
        _dwell(0), _clear(1800), _REOPEN,
    ]


PARAM_SCHEMAS: dict[str, list[ParamSpec]] = {
    "DQ-1": [
        ParamSpec("grace_intervals", "Grace", "int", 3, "polls", 1, 20, 1,
                  help="Polls of silence before a series counts as dropped"),
        ParamSpec("min_grace_s", "Grace floor", "int", 300, "s", 60, 3600, 60),
        ParamSpec("max_grace_s", "Grace ceiling", "int", 10_800, "s", 600, 86_400, 60),
        _samples("clear_samples", "Good samples to clear", 2, 20), _REOPEN,
    ],
    "DQ-2": [
        ParamSpec("window_s", "Window", "int", 1800, "s", 300, 7200, 60),
        ParamSpec("min_bad_fraction", "Non-GOOD share", "number", 0.5, None, 0.05, 1.0, 0.05),
        _samples("min_samples", "Samples at least", 5, 200),
        _dwell(0), _clear(300), _REOPEN,
    ],
    "DQ-3": [
        _samples("dwell_samples", "Samples to open", 3),
        _samples("clear_samples", "Samples to clear", 3), _REOPEN,
    ],
    "DQ-4": [
        _samples("window", "Hampel window", 11, 31),
        ParamSpec("k_mad", "Robust sigmas", "number", 5.0, "sigma", 2.0, 20.0, 0.5),
        ParamSpec("max_spikes_per_h", "Spikes per hour", "int", 6, None, 1, 120, 1,
                  help="A single spike is masked; a finding opens above this rate"),
        _dwell(0), _clear(3600), _REOPEN,
    ],
    "DQ-5": [
        _samples("min_samples", "Flat samples", 20, 64),
        _samples("clear_samples", "Moving samples to clear", 2, 20), _REOPEN,
    ],
    "CR-1": [
        ParamSpec("margin_k", "Over setpoint", "number", CRAH_SUPPLY_FAULT_MARGIN_K, "K",
                  0.5, 10, 0.1, help="Supply above setpoint before the rule counts"),
        ParamSpec("clear_margin_k", "Clears below", "number", 2.0, "K", 0.0, 10, 0.1),
        ParamSpec("valve_starved_pct", "CHW valve starved at", "number", 95.0, "%",
                  50, 100, 1),
        _RUN, _FIRE, _MAINT, _dwell(900), _clear(300), _REOPEN,
    ],
    "CR-2": [
        ParamSpec("min_delta_k", "Delta-T below", "number", 4.0, "K", 0.5, 15, 0.1),
        ParamSpec("clear_delta_k", "Clears above", "number", 5.0, "K", 0.5, 20, 0.1),
        ParamSpec("min_fan_pct", "Fan at least", "number", 30.0, "%", 0, 100, 1),
        _RUN, _FIRE, _MAINT, _dwell(900), _clear(600), _REOPEN,
    ],
    "CR-6": [*_hunting(), _RUN, _FIRE, _MAINT],
    "CH-1": [
        ParamSpec("margin_k", "Over setpoint", "number", 1.0, "K", 0.2, 10, 0.1),
        ParamSpec("clear_margin_k", "Clears below", "number", 0.5, "K", 0.0, 10, 0.1),
        ParamSpec("max_load_pct", "Below load", "number", 95.0, "%", 50, 100, 1,
                  help="At full load a chiller short of setpoint is capacity, not a fault"),
        ParamSpec("running_min_s", "Running at least", "int", 900, "s", 0, 7200, 60),
        ParamSpec("restart_exclusion_s", "After a start, ignore", "int", 480, "s",
                  0, 3600, 60),
        _MAINT, _dwell(900), _clear(300), _REOPEN,
    ],
    "CH-4": [
        ParamSpec("max_starts", "Starts allowed", "int", 3, None, 1, 20, 1),
        ParamSpec("window_s", "Per", "int", 3600, "s", 900, 14_400, 300),
        _dwell(0), _clear(3600), _REOPEN,
    ],
    "CH-5": [
        ParamSpec("rise_k_per_min", "Rising faster than", "number", 0.3, "K/min",
                  0.05, 5, 0.05),
        ParamSpec("trip_c", "Condenser trip", "number", 42.0, "C", 25, 60, 0.5,
                  help="Per site: the simulator's condenser trip point"),
        ParamSpec("horizon_s", "Projected within", "int", 600, "s", 60, 3600, 60),
        _MAINT, _samples("dwell_samples", "Samples to open", 2), _clear(600), _REOPEN,
    ],
    "PL-1": [
        ParamSpec("design_delta_k", "Design delta-T", "number", 5.5, "K", 2, 12, 0.1),
        ParamSpec("min_fraction", "Below fraction of design", "number", 0.6, None,
                  0.1, 1, 0.05),
        ParamSpec("flow_rising", "Only while flow rises", "bool", True),
        _MAINT, _dwell(1800), _clear(900), _REOPEN,
    ],
    "CT-2": [
        ParamSpec("fan_pct", "Fan at least", "number", 98.0, "%", 50, 100, 1),
        ParamSpec("approach_rise_k_per_h", "Approach rising faster than", "number", 0.5,
                  "K/h", 0.1, 10, 0.1),
        _MAINT, _dwell(1200), _clear(600), _REOPEN,
    ],
    "CT-3": [
        ParamSpec("fall_pct_per_h", "Basin falling faster than", "number", 2.0, "%/h",
                  0.2, 50, 0.1),
        _MAINT, _dwell(1800), _clear(1800), _REOPEN,
    ],
    "CD-1": [
        ParamSpec("valve_pct", "Facility valve at least", "number", 95.0, "%", 50, 100, 1),
        ParamSpec("over_setpoint_k", "Over setpoint", "number", CDU_SUPPLY_FAULT_MARGIN_K,
                  "K", 0.5, 10, 0.1),
        _MAINT, _dwell(300), _clear(300), _REOPEN,
    ],
    "CD-6": [
        ParamSpec("min_margin_k", "Above dew point by less than", "number", 2.0, "K",
                  0.5, 10, 0.1),
        ParamSpec("clear_margin_k", "Clears above", "number", 3.0, "K", 0.5, 15, 0.1),
        _MAINT, _dwell(600), _clear(600), _REOPEN,
    ],
    "VA-1": [
        ParamSpec("deviation_pct", "Command gap", "number", VALVE_DEVIATION_PCT, "%",
                  2, 50, 0.5),
        ParamSpec("clear_pct", "Clears below", "number", 5.0, "%", 0.5, 50, 0.5),
        ParamSpec("settle_s", "Command settled", "int", 120, "s", 0, 1800, 30),
        _MAINT, _dwell(600), _clear(300), _REOPEN,
    ],
    "VA-2": [*_hunting(), _MAINT],
    "PU-3": [
        ParamSpec("min_speed_pct", "Speed above", "number", 10.0, "%", 0, 100, 1),
        ParamSpec("min_flow_fraction", "Flow fraction", "number", 0.05, None,
                  0.01, 0.5, 0.01),
        ParamSpec("prove_s", "Prove within", "int", 60, "s", 0, 600, 10),
        _MAINT, _samples("dwell_samples", "Samples to open", 2),
        _samples("clear_samples", "Samples to clear", 3), _REOPEN,
    ],
    "RM-2": [
        ParamSpec("max_rate_k_per_h", "Rate above", "number", MAX_RATE_K_PER_H, "K/h",
                  1, 100, 0.5),
        ParamSpec("window_s", "Window", "int", 900, "s", 300, 3600, 60),
        _FIRE, _MAINT, _samples("dwell_samples", "Samples to open", 3), _clear(900),
        _REOPEN,
    ],
    "RM-3": [
        ParamSpec("margin_k", "Over room p90", "number", HOT_SPOT_MARGIN_K, "K",
                  0.5, 10, 0.1),
        ParamSpec("clear_margin_k", "Clears below", "number", 2.0, "K", 0.0, 10, 0.1),
        ParamSpec("min_room_sensors", "Room sensors at least", "int", 5, None, 3, 200, 1),
        _FIRE, _MAINT, _dwell(900), _clear(600), _REOPEN,
    ],
    "GN-1": [
        ParamSpec("min_drop_pct", "Drop at least", "number", 1.0, "%", 0.1, 20, 0.1),
        ParamSpec("window_s", "Per", "int", 3600, "s", 900, 86_400, 300),
        ParamSpec("not_running_min_s", "Stopped at least", "int", 1800, "s", 0, 86_400, 60),
        _dwell(1800), _clear(1800), _REOPEN,
    ],
    "AT-1": [
        ParamSpec("max_transfers_24h", "Transfers per day", "int", 2, None, 1, 50, 1),
        _dwell(0), _clear(86_400), _REOPEN,
    ],
    L5_ID: [
        ParamSpec("major_enabled", "Escalate roots to MAJOR", "bool", False,
                  help="Off until the Phase 2 gate passes (docs/33)"),
        ParamSpec("use_influence", "Use fitted CRAH zones", "bool", False),
        ParamSpec("window_s", "Correlation window", "int", 600, "s", 60, 3600, 60),
        ParamSpec("hold_down_s", "Incident hold-down", "int", 900, "s", 0, 7200, 60),
        ParamSpec("major_min_racks", "Racks for escalation", "int", 3, None, 1, 50, 1),
    ],
}


class ParamError(ValueError):
    """A PATCH or override params value the schema refuses (docs/30 section 12)."""

    def __init__(self, error: str, key: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.key = key


def param_schema(detector_id: str) -> list[dict[str, Any]]:
    return [p.as_dict() for p in PARAM_SCHEMAS.get(detector_id, [])]


def validate_params(detector_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Check a partial params dict against the detector's schema.

    Returns the coerced values (an int kind accepts 900.0 as 900). Raises
    ParamError('unknown_param' | 'param_out_of_range', key, message).
    """
    specs = {p.key: p for p in PARAM_SCHEMAS.get(detector_id, [])}
    out: dict[str, Any] = {}
    for key, value in params.items():
        spec = specs.get(key)
        if spec is None:
            raise ParamError("unknown_param", key,
                             f"{key!r} is not a tunable parameter of {detector_id}")
        out[key] = _coerce(spec, value)
    return out


def _coerce(spec: ParamSpec, value: Any) -> Any:
    def bad(why: str) -> ParamError:
        return ParamError("param_out_of_range", spec.key, f"{spec.key}: {why}")

    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise bad("must be true or false")
        return value
    if spec.kind == "enum":
        if value not in (spec.options or ()):
            raise bad(f"must be one of {', '.join(spec.options or ())}")
        return value
    # bool is an int subclass in Python; `true` is not a number here.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad("must be a number")
    if spec.kind == "int":
        if float(value) != int(value):
            raise bad("must be a whole number")
        value = int(value)
    else:
        value = float(value)
    if spec.min is not None and value < spec.min:
        raise bad(f"must be at least {spec.min}")
    if spec.max is not None and value > spec.max:
        raise bad(f"must be at most {spec.max}")
    return value


# --- L0 scope ----------------------------------------------------------------
#
# The numeric series data quality scores, per device type (docs/29 section 6.1
# counts). Every instance of a listed key is in scope. Booleans are not:
# they are change-logged with a 900 s heartbeat (ingest/changelog.py), so a
# silent boolean is normal and "no sample within grace" means nothing.
L0_METRICS: dict[str, tuple[str, ...]] = {
    "crah": ("supply_air_temp", "return_air_temp", "air_setpoint_temp", "fan_speed_pct",
             "valve_position_pct", "cooling_output_pct", "relative_humidity", "airflow",
             "power_draw"),
    "chiller": ("water_supply_temp", "water_return_temp", "water_setpoint_temp",
                "water_flow", "compressor_load_pct", "power_draw", "cooling_capacity",
                "cop", "water_pressure"),
    "cooling_tower": ("fan_speed_pct", "water_supply_temp", "water_return_temp",
                      "power_draw", "basin_level_pct", "makeup_water_flow", "vibration",
                      "outdoor_wet_bulb_temp"),
    "pump": ("pump_speed_pct", "water_flow", "water_pressure", "water_diff_pressure",
             "power_draw", "motor_temp", "vfd_frequency"),
    "cdu": ("water_supply_temp", "water_return_temp", "water_setpoint_temp", "water_flow",
            "valve_position_pct", "water_pressure", "thermal_load", "power_draw",
            "pump_speed_pct", "approach_temp", "filter_diff_pressure"),
    "valve": ("valve_position_pct", "motor_temp"),
    "sensor": ("inlet_temperature", "ambient_temperature", "relative_humidity",
               "dew_point"),
    "pdu": ("inlet_temperature", "ambient_temperature"),
    "ups": ("load_pct", "battery_temperature", "battery_runtime", "power_draw"),
    "transformer": ("oil_temperature", "load_pct"),
    "generator": ("fuel_level_pct", "coolant_temperature", "power_draw"),
    "server": ("inlet_temperature", "cpu_temperature", "fan_speed", "power_draw"),
}

#: Unit family for DQ-4's absolute MAD floor and DQ-5's coupled range, by
#: registry unit (contracts/metrics/registry.yaml).
UNIT_FAMILY = {"C": "temperature", "K": "temperature", "pct": "pct"}
