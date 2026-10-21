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
