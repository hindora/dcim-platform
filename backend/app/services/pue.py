"""Power Usage Effectiveness.

PUE is total facility energy divided by IT equipment energy, over a period.
Three things are easy to get wrong and all three are worth stating in the
response rather than in a footnote nobody reads.

**Energy, not power.** A ratio of instantaneous powers is not PUE. It swings
with the compressor duty cycle and with every fan that stages, and two readings
a minute apart can differ by a tenth. The Green Grid definition is energy over
a period; this fleet's meters are kWh counters, so use them and fall back to
power only when they cannot be read.

**The measurement level changes the number.** IT energy taken at the UPS output
includes distribution losses that IT energy taken at the equipment inlet does
not, so the same site reports a lower PUE at Category 3 than at Category 1.
Comparing an unlabelled PUE with someone else's is meaningless, which is why
the category travels with the value.

**A PUE below 1.0 is not a very good datacenter.** It means the IT figure is
too big or the facility figure too small - a meter counted twice, a scope that
excluded cooling. Reporting it as an achievement is worse than reporting
nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

log = get_logger("pue")

# Total facility energy: everything crossing the boundary into the site.
FACILITY_TYPES = ["utility_feed"]

# IT energy at the UPS output. On this fleet the UPS Modbus point is
# Energy_Delivered - energy OUT of the UPS - and the UPS feeds only the IT
# distribution path (RPP -> PDU -> load), while mechanical plant hangs off the
# MCC upstream of it. That is the definition of Green Grid Category 1.
IT_TYPES_L1 = ["ups"]

# What each category means, kept next to the types so the two cannot drift.
CATEGORY_POINT = {
    1: "UPS output (Energy_Delivered)",
    2: "PDU output",
    3: "IT equipment inlet",
}

# Below 1.0 is impossible: facility energy includes IT energy by definition.
# Above this, the number is not wrong so much as it is a different problem.
IMPLAUSIBLE_LOW = 1.0
UNUSUALLY_HIGH = 3.0

# How close to now a window must end before instantaneous power may stand in
# for it. Present power says nothing about last Tuesday, and answering a
# historical question with today's reading is not a degraded answer - it is a
# different answer wearing the same label.
POWER_FALLBACK_WINDOW_S = 900


def classify(value: float | None) -> tuple[bool, str | None]:
    """Is this PUE believable, and if not, what is the likely cause."""
    if value is None:
        return False, "no value"
    if value < IMPLAUSIBLE_LOW:
        return False, (
            f"PUE {value:.3f} is below 1.0, which is physically impossible - "
            "facility energy contains IT energy. Either the IT meter is "
            "double-counted or the facility scope is missing a load")
    if value > UNUSUALLY_HIGH:
        return True, (
            f"PUE {value:.2f} is unusually high; plausible during very low IT "
            "load, when fixed facility overhead dominates")
    return True, None


async def compute(session, *, start: datetime, end: datetime,
                  datacenter_id: str | None = None) -> dict[str, Any]:
    """PUE over a window, by energy where possible and power where not."""
    from app.repositories import pue as repo

    total = await repo.energy_delta(
        session, device_types=FACILITY_TYPES, start=start, end=end,
        datacenter_id=datacenter_id)
    it = await repo.energy_delta(
        session, device_types=IT_TYPES_L1, start=start, end=end,
        datacenter_id=datacenter_id)

    window_s = max(1.0, (end - start).total_seconds())
    result: dict[str, Any] = {
        "start": start, "end": end, "datacenter_id": datacenter_id,
    }

    if total["kwh"] > 0 and it["kwh"] > 0:
        value = total["kwh"] / it["kwh"]
        ok, note = classify(value)
        result.update({
            "pue": round(value, 3),
            "method": "energy",
            "category": 1,
            "measurement_point": CATEGORY_POINT[1],
            "total_facility_kwh": round(total["kwh"], 1),
            "it_kwh": round(it["kwh"], 1),
            "plausible": ok,
            "note": note,
            "counter_resets": total["resets"] + it["resets"],
            "meters": {"facility": len(total["devices"]), "it": len(it["devices"])},
        })
        log.info("pue computed", method="energy", pue=result["pue"],
                 dc=datacenter_id)
        return result

    # Energy unusable: no counters in the window, or a window so short that
    # nothing incremented. An instantaneous ratio is a different and weaker
    # claim, so it is labelled as one - and it is only offered for a window
    # that is asking about now.
    age_s = (datetime.now(UTC) - end).total_seconds()
    if age_s > POWER_FALLBACK_WINDOW_S:
        result.update({
            "pue": None, "method": None, "plausible": False,
            "note": (
                f"no energy counter data in this window, and it ended "
                f"{age_s / 3600:.1f} h ago - present power cannot answer a "
                f"question about a past period"),
        })
        return result

    p_total = await repo.power_now(session, device_types=FACILITY_TYPES,
                                   datacenter_id=datacenter_id)
    p_it = await repo.power_now(session, device_types=IT_TYPES_L1,
                                datacenter_id=datacenter_id)
    if p_total["watts"] > 0 and p_it["watts"] > 0:
        value = p_total["watts"] / p_it["watts"]
        ok, note = classify(value)
        result.update({
            "pue": round(value, 3),
            "method": "power",
            "category": 1,
            "measurement_point": CATEGORY_POINT[1],
            "total_facility_kw": round(p_total["watts"] / 1000.0, 1),
            "it_kw": round(p_it["watts"] / 1000.0, 1),
            "plausible": ok,
            "note": (note + "; " if note else "")
                    + "instantaneous power ratio, not an energy-based PUE - it "
                      "swings with compressor and fan staging",
            "meters": {"facility": len(p_total["devices"]),
                       "it": len(p_it["devices"])},
        })
        log.info("pue computed", method="power", pue=result["pue"],
                 dc=datacenter_id)
        return result

    result.update({
        "pue": None, "method": None, "plausible": False,
        "note": ("no usable energy or power readings in this window"
                 if window_s > 60 else
                 "window too short for the meters to have incremented"),
    })
    return result
