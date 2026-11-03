"""How each L3 detector learns and is judged (docs/35): one table, read by the
nightly fit (analytics/baselines.py) and by the sweep (detectors/residual.py),
so the two can never disagree about what a baseline is OF.

Three shapes:

  profile  the reading itself, by hour of day (SV-3, UP-3, PD-2, NW-1)
  ols      the reading given its drivers on the same device, a least-squares
           fit on one or more of them, each optionally transformed (SV-4, SV-1,
           SV-2, UP-2)
  gap      the reading minus a room-wide reference, by hour of day (TH-1, UP-1):
           what moves with the whole room is not this device's anomaly
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Driver:
    key: str
    #: 'none', or 'inverse': x -> 100 / max(x, 1). Battery runtime falls as
    #: 1/load, so a straight line on the inverse is the honest model.
    transform: str = "none"


@dataclass(frozen=True)
class Spec:
    kind: str                                   # 'profile' | 'ols' | 'gap'
    y: str                                      # the metric judged
    drivers: tuple[Driver, ...] = ()            # ols only
    ref: str | None = None                      # gap: the room reference metric
    ref_types: tuple[str, ...] = ()             # gap: device types that carry it
    ref_ctx: str | None = None                  # gap: rules.Context attribute
    of: str | None = None                       # gap: the model tag
    unit: str = ""
    what: str = ""
    direction: str = "both"


SPECS: dict[str, Spec] = {
    "SV-3": Spec("profile", "cpu_utilization", unit=" %", what="CPU"),
    "UP-3": Spec("profile", "load_pct", unit=" %", what="load"),
    "PD-2": Spec("profile", "phase_imbalance_pct", unit=" %", what="phase imbalance",
                 direction="high"),
    "NW-1": Spec("profile", "if_in_error_rate", unit=" /s", what="receive errors",
                 direction="high"),
    "SV-4": Spec("ols", "power_draw", (Driver("cpu_utilization"),), unit=" W",
                 what="drawing"),
    "SV-1": Spec("ols", "cpu_temperature",
                 (Driver("cpu_utilization"), Driver("inlet_temperature")),
                 unit=" C", what="CPU at", direction="high"),
    "SV-2": Spec("ols", "fan_speed",
                 (Driver("inlet_temperature"), Driver("cpu_utilization")),
                 unit=" rpm", what="fan at", direction="high"),
    "UP-2": Spec("ols", "battery_runtime", (Driver("load_pct", "inverse"),),
                 unit=" min", what="battery runtime", direction="low"),
    "TH-1": Spec("gap", "inlet_temperature", ref="supply_air_temp",
                 ref_types=("crah", "crac"), ref_ctx="room_supply",
                 of="inlet_minus_room_supply", direction="high"),
    "UP-1": Spec("gap", "battery_temperature", ref="ambient_temperature",
                 ref_types=("sensor",), ref_ctx="room_ambient",
                 of="battery_minus_room_ambient", direction="high"),
}

#: How a driver is named in a sentence.
DRIVER_WORDS = {"cpu_utilization": "% CPU", "inlet_temperature": "C intake",
                "load_pct": "% load"}


def transform(x: float, how: str) -> float:
    if how == "inverse":
        return 100.0 / max(x, 1.0)
    return x
