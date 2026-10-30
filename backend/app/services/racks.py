"""One rack, read the way the Power and Thermal pages read it - for one rack.

A rack is something people manage in its own right: its power budget, its
feeds, the air it breathes, the space left in it. The estate pages answer
those for every rack at once and the user clicks one; this answers them for
that rack alone, with the same arithmetic (the estate service's own rack
row and helpers) so the page a click lands on never disagrees with the row
that was clicked. Each read is the estate read scoped to the rack, which is
what keeps a 30-second refresh of one page from costing the whole estate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import POWER_BANDS
from app.core.ashrae import envelope_for, grade_temperature
from app.repositories import estate as estate_repo
from app.repositories import rack_detail as repo
from app.repositories import racks as rack_repo
from app.schemas import RackSummary
from app.services import estate as estate_service

#: Worst-first, for the alarm roll-up over the rack's devices.
_SEV_RANK = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "WARNING": 3}

#: The hot mirror's numeric columns arrive as Decimals, which Pydantic hands
#: to JSON as strings; the page wants numbers it can format.
_NUMERIC = ("power_w", "inlet_temp_c", "cpu_util_pct")


def _plain(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for k in _NUMERIC:
        if out.get(k) is not None:
            out[k] = float(out[k])
    return out


async def rack_detail(session: AsyncSession, rack_id: str) -> dict[str, Any] | None:
    rack = await rack_repo.get_rack(session, rack_id)
    if rack is None:
        return None
    devices = [_plain(d) for d in await rack_repo.rack_devices(session, rack_id)]
    return {
        "rack": RackSummary(**{k: v for k, v in rack.items() if k in RackSummary.model_fields}),
        "space": space_block(rack, devices),
        "alarms": alarm_block(devices),
        "power": await power_block(session, rack_id, devices),
        "thermal": thermal_block(await repo.thermal_points(session, rack_id)),
        "devices": devices,
    }


# ------------------------------------------------------------------ space

def space_block(rack: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Rack units used and free, and the largest run of free ones: where the
    next 2U server fits is a different question from how many U are free."""
    u_total = int(rack.get("u_height") or 42)
    racked = [d for d in devices if d.get("u_start")]
    occupied = [(int(d["u_start"]), int(d.get("u_height") or 1)) for d in racked]
    free_blocks = rack_repo.compute_free_blocks(u_total, occupied)
    largest = max(free_blocks, key=lambda b: b["u_height"], default=None)
    u_used = sum(h for _, h in occupied)
    return {
        "u_total": u_total,
        "u_used": u_used,
        "u_free": max(u_total - u_used, 0),
        "largest_free": largest,
        "free_blocks": free_blocks,
        "racked": len(racked),
        # Vertical strips and strapped-on probes: in the rack, at no U.
        "zero_u": len(devices) - len(racked),
        "by_category": _count_by(devices, "category"),
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = r.get(key) or "other"
        out[k] = out.get(k, 0) + 1
    return out


# ----------------------------------------------------------------- alarms

def alarm_block(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """Open alarms over everything in the rack, and the worst of them."""
    alarmed = [d for d in devices if int(d.get("active_alarms") or 0) > 0]
    worst = min((d.get("max_severity") or "CLEAR" for d in alarmed),
                key=lambda s: _SEV_RANK.get(s, 9), default="CLEAR")
    return {
        "open": sum(int(d.get("active_alarms") or 0) for d in alarmed),
        "worst": worst if alarmed else "CLEAR",
        "devices": len(alarmed),
        "offline": sum(1 for d in devices if d.get("status") == "OFFLINE"),
    }


# ------------------------------------------------------------------ power

async def power_block(session: AsyncSession, rack_id: str,
                      devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Power page's rack row, for this rack: draw against rating and
    budget, the week's p95 and what it leaves, each strip with its legs and
    banks, the loads on one cord, and what a side carries if the other is
    lost. Null only for a rack the inventory does not know."""
    rows = await estate_repo.power_racks_live(session, rack_id=rack_id)
    if not rows:
        return None
    inv = await estate_repo.power_inventory(session, rack_id=rack_id)
    row = estate_service._power_rack_row(rows[0], inv, peak=False)

    p = await estate_repo.power_p95(session, rack_id=rack_id)
    p95 = p["racks"].get(rack_id)
    legs = (await estate_repo.power_rack_legs(session, rack_id=rack_id)).get(rack_id, [])
    psu = (await estate_repo.power_rack_psu_failed(session, rack_id=rack_id)).get(rack_id, [])
    strips = (await estate_repo.power_rack_strip_state(session, rack_id=rack_id)).get(rack_id, [])
    # The cabling record only where a strip meters its receptacles.
    outlet_loads: dict[str, dict[int, str]] = {}
    if any(s.get("outlet_a") for s in strips):
        outlet_loads = await estate_repo.power_rack_outlet_loads(session, rack_id=rack_id)

    worst_imbalance = [s["imbalance_pct"] for s in legs if s.get("imbalance_pct") is not None]
    banks = estate_service.rack_bank_load(strips)
    by_id = {d["id"]: d for d in devices}
    row.update({
        "p95_kw": p95,
        "p95_days": p["days"],
        "headroom_kw": (None if p95 is None or not row.get("rated_kw")
                        else round(row["rated_kw"] - p95, 2)),
        "stranded_kw": estate_service._stranded(row.get("allocated_kw"), p95),
        "legs": legs,
        "imbalance_pct": max(worst_imbalance) if worst_imbalance else None,
        "psu_failed": psu,
        "banks": banks[:6],
        "bank_worst_pct": banks[0]["pct"] if banks else None,
        "failover": estate_service.rack_failover(strips),
        "outlets": estate_service.rack_outlet_metering(strips, outlet_loads),
        # Each strip as the feeds card draws it: which side, how loaded, and
        # whether the strip itself is answering - a dead strip is a dead feed.
        "strips": [{
            "id": s["strip_id"], "name": s["strip"], "side": s.get("side"),
            "rated_kw": s.get("rated_kw"), "kw": s.get("kw"),
            "legs": s.get("legs") or [], "v_ln": s.get("v_ln"),
            "status": by_id.get(s["strip_id"], {}).get("status", "UNKNOWN"),
            "max_severity": by_id.get(s["strip_id"], {}).get("max_severity", "CLEAR"),
        } for s in sorted(strips, key=lambda s: (s.get("side") or "", s["strip"]))],
        "thresholds": {
            "warn_pct": POWER_BANDS["warn_pct"],
            "critical_pct": POWER_BANDS["critical_pct"],
            "bank_warn_pct": estate_service.BANK_WARN_PCT,
            "leg_imbalance_warn_pct": estate_service.LEG_IMBALANCE_WARN_PCT,
            "outlet_warn_pct": estate_service.OUTLET_WARN_PCT,
        },
    })
    # Feeds that meet upstream: the single point of failure a 2N rack hides.
    await estate_service._attach_shared_upstream(session, [row])
    return estate_service._strip(row)


# ---------------------------------------------------------------- thermal

def thermal_block(points: list[dict[str, Any]]) -> dict[str, Any]:
    """The air in the rack by height, graded against the room's ASHRAE class.

    Intake per device is what the Thermal page pools: a server's inlet, a
    switch's component temperature (the intake it does not measure), a
    probe's or strip head's ambient in the cold aisle. The hot-spot rule is
    the room index's: the hottest intake over the recommended ceiling is a
    hot spot, over the allowable one a hot spot out of envelope.
    """
    env = envelope_for(points[0].get("ashrae_class") if points else None)
    out_points: list[dict[str, Any]] = []
    intakes: list[float] = []
    exhausts: list[float] = []
    rhs: list[float] = []
    for p in points:
        cat, dtype = p.get("category"), p.get("device_type")
        intake = exhaust = None
        if cat in ("it", "network"):
            kind = "device"
            intake = p.get("inlet_c")
            if intake is None and cat == "network":
                intake = p.get("component_c")
            exhaust = p.get("exhaust_c")
        elif dtype == "sensor":
            kind = "probe"
            intake = p.get("ambient_c")
        elif dtype == "pdu":
            kind = "strip"
            intake = p.get("ambient_c")
        else:
            continue
        if intake is None and exhaust is None:
            continue
        if intake is not None:
            intakes.append(float(intake))
        if exhaust is not None:
            exhausts.append(float(exhaust))
        if p.get("rh_pct") is not None:
            rhs.append(float(p["rh_pct"]))
        out_points.append({
            "id": p["id"], "name": p["name"], "device_type": dtype, "kind": kind,
            "u_start": p.get("u_start"), "u_height": p.get("u_height"),
            "intake_c": None if intake is None else round(float(intake), 1),
            "exhaust_c": None if exhaust is None else round(float(exhaust), 1),
            "cpu_c": None if p.get("cpu_c") is None else round(float(p["cpu_c"]), 1),
            "rh_pct": None if p.get("rh_pct") is None else round(float(p["rh_pct"]), 1),
            "grade": grade_temperature(float(intake), env) if intake is not None else None,
            "read_at": p.get("read_at"),
        })
    i_mean = _mean(intakes)
    e_mean = _mean(exhausts)
    i_max = max(intakes) if intakes else None
    hot = None
    if i_max is not None:
        if i_max > env.allow_high_c:
            hot = "out"
        elif i_max > env.rec_high_c:
            hot = "allowable"
    return {
        "ashrae": {"class": env.name, "rec_low_c": env.rec_low_c, "rec_high_c": env.rec_high_c,
                   "allow_low_c": env.allow_low_c, "allow_high_c": env.allow_high_c},
        "intake_mean_c": i_mean, "intake_max_c": i_max,
        "intake_min_c": min(intakes) if intakes else None,
        "exhaust_mean_c": e_mean, "exhaust_max_c": max(exhausts) if exhausts else None,
        "delta_t_k": (None if i_mean is None or e_mean is None
                      else round(e_mean - i_mean, 1)),
        "rh_mean_pct": _mean(rhs),
        "sensors": len(intakes),
        "in_band": sum(1 for t in intakes if env.rec_low_c <= t <= env.rec_high_c),
        "hot": hot,
        "points": out_points,
    }


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 1) if xs else None
