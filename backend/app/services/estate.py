"""The estate pages: thermal, power, utilisation, and the alert drill-downs.

One shape for all three. Each returns `{scope, totals, rows[], window, notes}`
so a single table component on the front end can render any of them, and each
row carries its site id so the same payload serves both the SITES and the ROOMS
view without a second request.

Two rules run through the whole module:

* A number with no instrument behind it is `null` with a `note`, never zero.
  Rooms with no rack sensors, sites with no design rating and DC-bus power
  nobody meters all take this path.
* Site rows are folded from room rows by weight, never by averaging averages.
  A room with four probes must not outvote one with four hundred.
* Rooms are labelled white space or facility, but facility rooms are never
  dropped from a site total. Two thirds of a site's cooling draw stands in its
  plant room; excluding it to make the room list tidier would move PUE by a
  third and describe a plant nobody built. The rows a page SHOWS and the
  arithmetic it does are separate decisions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import DETECTIONS
from app.repositories import estate as repo

# ASHRAE TC 9.9 recommended envelope for class A1-A4 equipment intake air.
# Compliance on this page means "inside the RECOMMENDED band", which is a
# tighter test than the allowable envelope a device will survive - the point of
# the number is efficiency and headroom, not whether anything melted.
BAND_LOW_C = 18.0
BAND_HIGH_C = 27.0

# Buckets for windowed power. Short ranges get fine buckets; a month at five
# minutes would be nine thousand buckets per room for a table that shows one
# average and one peak.
# Passed to time_bucket as an interval, so these are timedeltas rather than
# strings: asyncpg types the parameter from the value, and a string bound to an
# interval column fails at execute time, not at import.
_BUCKET_STEPS = ((timedelta(days=2), timedelta(minutes=5)),
                 (timedelta(days=14), timedelta(minutes=30)),
                 (timedelta(days=90), timedelta(hours=1)))
_BUCKET_FALLBACK = timedelta(days=1)


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _pct(part: Any, whole: Any) -> float | None:
    p, w = _f(part), _f(whole)
    if p is None or not w:
        return None
    return round(p / w * 100.0, 1)


def _delta(now: float | None, before: float | None) -> float | None:
    """Change against the comparison window, or None if either side is missing.

    Explicitly not zero when the comparison window is empty: "unchanged" and
    "nothing to compare with" are different answers, and the arrow the UI draws
    for them is different too.
    """
    if now is None or before is None:
        return None
    return round(now - before, 2)


def _bucket_for(start: datetime, end: datetime) -> timedelta:
    span = end - start
    for limit, bucket in _BUCKET_STEPS:
        if span <= limit:
            return bucket
    return _BUCKET_FALLBACK


def _day_window(d: date) -> tuple[datetime, datetime]:
    """A calendar day in UTC.

    Deliberately UTC rather than site-local: the estate spans time zones, and a
    table whose rows each cover a different 24 hours cannot be compared down a
    column. The window is stated in the response so the reader knows which day
    they are looking at.
    """
    start = datetime.combine(d, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


# --------------------------------------------------------------------- thermal


async def thermal(session: AsyncSession, *, focus: date | None = None,
                  compare: date | None = None,
                  mode: str = "daily") -> dict[str, Any]:
    """Intake temperature, spread and compliance per room and per site."""
    if mode == "live":
        end = datetime.now(UTC)
        f0, f1 = end - timedelta(hours=1), end
        c0, c1 = f0 - timedelta(hours=1), f0
        window = {"mode": "live", "focus_start": f0, "focus_end": f1,
                  "compare_start": c0, "compare_end": c1,
                  "label": "last hour", "compare_label": "previous hour"}
    else:
        # Today, not yesterday. The page is opened to see what the estate is
        # doing, and a default that lands on a day already closed makes the
        # most common visit start with a date change.
        focus = focus or datetime.now(UTC).date()
        compare = compare or (focus - timedelta(days=1))
        f0, f1 = _day_window(focus)
        c0, c1 = _day_window(compare)
        window = {"mode": "daily", "focus_start": f0, "focus_end": f1,
                  "compare_start": c0, "compare_end": c1,
                  "label": focus.isoformat(), "compare_label": compare.isoformat()}

    raw = await repo.thermal(session, focus_start=f0, focus_end=f1,
                             compare_start=c0, compare_end=c1,
                             low_c=BAND_LOW_C, high_c=BAND_HIGH_C)

    rows: list[dict[str, Any]] = []
    for r in raw:
        n = int(r["f_n"] or 0)
        prev_n = int(r["c_n"] or 0)
        avg = round(float(r["f_sum"]) / n, 1) if n else None
        prev_avg = round(float(r["c_sum"]) / prev_n, 1) if prev_n else None
        rows.append({
            "id": r["room_id"],
            "kind": "room",
            "name": r["room_name"],
            "floor": r["floor"],
            "room_type": r["room_type"],
            "room_class": r["room_class"],
            "site_id": r["datacenter_id"],
            "site_code": r["site_code"],
            "site_name": r["site_name"],
            "rack_count": int(r["rack_count"] or 0),
            "avg_c": avg,
            "max_c": _f(r["f_max"]) and round(_f(r["f_max"]), 1),
            "compliance_pct": _pct(r["f_in_band"], n) if n else None,
            "samples": n,
            "delta_avg": _delta(avg, prev_avg),
            "delta_max": _delta(_f(r["f_max"]) and round(_f(r["f_max"]), 1),
                                _f(r["c_max"]) and round(_f(r["c_max"]), 1)),
            # Said once per row, so a reader never has to guess whether a blank
            # cell means "cool" or "nobody is measuring".
            "note": None if n else "no rack intake sensor reported in this window",
            # The parts, so sites can be folded without averaging averages.
            "_sum": _f(r["f_sum"]) or 0.0, "_n": n,
            "_in_band": int(r["f_in_band"] or 0),
            "_prev_sum": _f(r["c_sum"]) or 0.0, "_prev_n": prev_n,
            "_max": _f(r["f_max"]), "_prev_max": _f(r["c_max"]),
        })

    sites = _fold_thermal_sites(rows)
    totals = _fold_thermal_total(rows)
    return {
        "window": window,
        "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
                 "basis": "ASHRAE TC 9.9 recommended envelope for intake air"},
        "totals": totals,
        "sites": sites,
        "rooms": [_strip(r) for r in rows],
        "notes": [
            "Relative humidity is not shown: no humidity instrument exists "
            "anywhere in the estate, and deriving one from dry and wet bulb "
            "would publish a calculation as a reading.",
        ],
    }


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _fold_thermal_sites(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for r in rooms:
        s = by_site.setdefault(r["site_id"], {
            "id": r["site_id"], "kind": "site", "name": r["site_name"],
            "site_id": r["site_id"], "site_code": r["site_code"],
            "site_name": r["site_name"], "room_count": 0, "rack_count": 0,
            "_sum": 0.0, "_n": 0, "_in_band": 0, "_prev_sum": 0.0,
            "_prev_n": 0, "_max": None, "_prev_max": None,
        })
        s["room_count"] += 1
        s["rack_count"] += r["rack_count"]
        for key in ("_sum", "_n", "_in_band", "_prev_sum", "_prev_n"):
            s[key] += r[key]
        for key in ("_max", "_prev_max"):
            if r[key] is not None:
                s[key] = r[key] if s[key] is None else max(s[key], r[key])

    out = []
    for s in by_site.values():
        avg = round(s["_sum"] / s["_n"], 1) if s["_n"] else None
        prev = round(s["_prev_sum"] / s["_prev_n"], 1) if s["_prev_n"] else None
        mx = None if s["_max"] is None else round(s["_max"], 1)
        prev_mx = None if s["_prev_max"] is None else round(s["_prev_max"], 1)
        out.append(_strip({**s,
                           "avg_c": avg, "max_c": mx,
                           "compliance_pct": _pct(s["_in_band"], s["_n"]) if s["_n"] else None,
                           "samples": s["_n"],
                           "delta_avg": _delta(avg, prev),
                           "delta_max": _delta(mx, prev_mx),
                           "note": None if s["_n"] else "no rack intake sensor reported"}))
    return sorted(out, key=lambda r: r["site_code"])


def _fold_thermal_total(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    n = sum(r["_n"] for r in rooms)
    in_band = sum(r["_in_band"] for r in rooms)
    maxes = [r["_max"] for r in rooms if r["_max"] is not None]
    total = sum(r["_sum"] for r in rooms)
    white = [r for r in rooms if r["room_class"] == "white_space"]
    return {
        "avg_c": round(total / n, 1) if n else None,
        "max_c": round(max(maxes), 1) if maxes else None,
        "compliance_pct": _pct(in_band, n) if n else None,
        "samples": n,
        # Reporting is counted over WHITE SPACE only. Rack intake sensors exist
        # where racks do; counting a generator room as a room that failed to
        # report made the ratio read as a fleet of dead sensors.
        "rooms_reporting": sum(1 for r in white if r["_n"]),
        "rooms": len(white),
        "facility_rooms": len(rooms) - len(white),
    }


# ----------------------------------------------------------------------- power


async def power(session: AsyncSession, *, start: datetime | None = None,
                end: datetime | None = None, mode: str = "average",
                live: bool = False) -> dict[str, Any]:
    """Room and site power, split IT / cooling / other, with PUE per row."""
    if live:
        raw = await repo.power_live(session)
        window = {"mode": "live", "label": "now"}
    else:
        end = end or datetime.now(UTC)
        start = start or (end - timedelta(days=1))
        span = end - start
        raw = await repo.power_window(session, start=start, end=end,
                                      compare_start=start - span,
                                      compare_end=start,
                                      bucket=_bucket_for(start, end))
        window = {"mode": mode, "start": start, "end": end,
                  "bucket_seconds": int(_bucket_for(start, end).total_seconds()),
                  "compare_start": start - span, "compare_end": start,
                  "label": f"{start.date().isoformat()} to {end.date().isoformat()}"}

    peak = mode == "peak" and not live
    rows = [_power_row(r, peak=peak) for r in raw]
    sites = _fold_power_sites(rows)
    return {
        "window": window,
        "totals": _fold_power_total(rows),
        "sites": sites,
        "rooms": [_strip(r) for r in rows],
        "notes": [
            "IT(DC) is blank throughout: nothing meters a DC bus in this "
            "estate, so the column exists for parity with sites that do.",
            "Peak is coincident - loads are summed per bucket before the "
            "maximum is taken, so the figure is one that actually occurred.",
        ],
    }


def _power_row(r: dict[str, Any], *, peak: bool) -> dict[str, Any]:
    def pick(avg_key: str, peak_key: str) -> float | None:
        v = r.get(peak_key if peak else avg_key)
        return None if v is None else round(float(v), 1)

    it = pick("avg_it", "peak_it")
    cooling = pick("avg_cooling", "peak_cooling")
    other = pick("avg_other", "peak_other")
    total = pick("avg_total", "peak_total")
    if total is None and it is not None:
        total = round(it + (cooling or 0) + (other or 0), 1)

    prev = _f(r.get("prev_total"))
    return {
        "id": r["room_id"],
        "kind": "room",
        "name": r["room_name"],
        "floor": r["floor"],
        "room_class": r["room_class"],
        "site_id": r["datacenter_id"],
        "site_code": r["site_code"],
        "site_name": r["site_name"],
        "total_kw": total,
        "it_ac_kw": it,
        # No DC bus is metered anywhere in this estate. Null, so the UI shows a
        # dash rather than a zero that would read as "nothing plugged in".
        "it_dc_kw": None,
        "cooling_kw": cooling,
        "other_kw": other,
        "pue": round(total / it, 3) if total and it else None,
        "delta_total": _delta(total, None if prev is None else round(prev, 1)),
        "note": None if total is not None else "no power meter reported here",
        "_it": it, "_cooling": cooling, "_other": other, "_total": total,
        "_prev": None if prev is None else round(prev, 1),
    }


def _fold_power_sites(rooms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for r in rooms:
        s = by_site.setdefault(r["site_id"], {
            "id": r["site_id"], "kind": "site", "name": r["site_name"],
            "site_id": r["site_id"], "site_code": r["site_code"],
            "site_name": r["site_name"], "room_count": 0,
            "_it": None, "_cooling": None, "_other": None, "_total": None,
            "_prev": None,
        })
        s["room_count"] += 1
        for key in ("_it", "_cooling", "_other", "_total", "_prev"):
            if r[key] is not None:
                s[key] = r[key] if s[key] is None else round(s[key] + r[key], 1)

    out = []
    for s in by_site.values():
        total, it = s["_total"], s["_it"]
        out.append(_strip({**s,
                           "total_kw": total, "it_ac_kw": it, "it_dc_kw": None,
                           "cooling_kw": s["_cooling"], "other_kw": s["_other"],
                           "pue": round(total / it, 3) if total and it else None,
                           "delta_total": _delta(total, s["_prev"]),
                           "note": None if total is not None else "no power meter reported"}))
    return sorted(out, key=lambda r: r["site_code"])


def _fold_power_total(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str, subset: list[dict[str, Any]] | None = None) -> float | None:
        vals = [r[key] for r in (subset or rooms) if r[key] is not None]
        return round(sum(vals), 1) if vals else None

    it, all_kw = total("_it"), total("_total")
    facility = [r for r in rooms if r["room_class"] != "white_space"]
    return {"total_kw": all_kw, "it_ac_kw": it, "it_dc_kw": None,
            "cooling_kw": total("_cooling"), "other_kw": total("_other"),
            "pue": round(all_kw / it, 3) if all_kw and it else None,
            "rooms_reporting": sum(1 for r in rooms if r["_total"] is not None),
            "rooms": len(rooms),
            # What the facility rooms contribute. The UI hides those rows by
            # default, and a header that does not match the visible rows has to
            # explain the difference rather than let a reader find it.
            "facility": {
                "rooms": len(facility),
                "total_kw": total("_total", facility),
                "cooling_kw": total("_cooling", facility),
            }}


# ----------------------------------------------------------------- utilisation


async def utilisation(session: AsyncSession) -> dict[str, Any]:
    """Space, power and cooling used against what is installed."""
    raw = await repo.utilisation(session)
    design = await repo.site_design(session)

    rows = []
    for r in raw:
        total_u, used_u = _f(r["total_u"]) or 0.0, _f(r["used_u"]) or 0.0
        it_kw = _f(r["it_kw"]) or 0.0
        design_kw = _f(r["design_it_kw"])
        supply_kw = _f(r["supply_rated_kw"])

        if design_kw:
            power_cap, power_basis = design_kw, "room design IT load"
        elif supply_kw:
            # Installed, not usable. On a 2N floor roughly half of this exists
            # to carry the load when the other half is gone, so a reader must
            # not take 60% here as 40% of headroom.
            power_cap = supply_kw
            power_basis = (f"nameplate of {int(r['supply_units'] or 0)} PDU/RPP "
                           "installed - not de-rated for redundancy")
        else:
            power_cap, power_basis = None, "no design rating and no rated PDU or RPP here"

        cooling_cap = _f(r["cooling_capacity_kw"])
        designed = int(r["designed_racks"] or 0) or None
        area = (_f(r["width_m"]) or 0) * (_f(r["depth_m"]) or 0) or None
        rows.append({
            "id": r["room_id"], "kind": "room", "name": r["room_name"],
            "floor": r["floor"], "room_class": r["room_class"],
            "site_id": r["datacenter_id"],
            "site_code": r["site_code"], "site_name": r["site_name"],
            "rack_count": int(r["rack_count"] or 0),
            # Build-out: racks standing against rack positions the room was
            # drawn with. A hall can be 35% full by U and 12% built out, and
            # those are different conversations - one about the racks you have,
            # one about the floor you have not filled yet.
            "designed_racks": designed,
            "built_out_pct": _pct(r["rack_count"], designed),
            "floor_area_m2": None if area is None else round(area, 1),
            "space_pct": _pct(used_u, total_u), "space_used_u": used_u,
            "space_total_u": total_u,
            "power_pct": _pct(it_kw, power_cap), "power_used_kw": round(it_kw, 1),
            "power_capacity_kw": None if power_cap is None else round(power_cap, 1),
            "power_basis": power_basis,
            # IT heat against installed cooling. The plant's own draw is not a
            # load on itself, so cooling kW is excluded from the numerator.
            "cooling_pct": _pct(it_kw, cooling_cap),
            "cooling_used_kw": round(it_kw, 1),
            "cooling_capacity_kw": None if cooling_cap is None else round(cooling_cap, 1),
            "cooling_basis": (f"{int(r['cooling_units'] or 0)} unit(s) reporting "
                              "rated capacity" if cooling_cap
                              else "no cooling unit here reports a rated capacity"),
            "_used_u": used_u, "_total_u": total_u, "_it_kw": it_kw,
            "_power_cap": power_cap, "_cooling_cap": cooling_cap,
        })

    return {
        "totals": _fold_util_total(rows),
        "sites": _fold_util_sites(rows, design),
        "rooms": [_strip(r) for r in rows],
        "notes": [
            "Space is exact - it comes from inventory. Power and cooling are "
            "measured against whatever rating could be found, and each row "
            "says which one it used.",
            "Space and build-out cover WHITE SPACE only: plant and switchrooms "
            "hold cabinets, not rack capacity. Their electrical load is still "
            "counted in every kW figure here.",
        ],
    }


def _fold_util_sites(rooms: list[dict[str, Any]],
                     design: dict[str, Any]) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for r in rooms:
        s = by_site.setdefault(r["site_id"], {
            "id": r["site_id"], "kind": "site", "name": r["site_name"],
            "site_id": r["site_id"], "site_code": r["site_code"],
            "site_name": r["site_name"], "room_count": 0, "rack_count": 0,
            "facility_racks": 0,
            "_used_u": 0.0, "_total_u": 0.0, "_it_kw": 0.0,
            "_power_cap": 0.0, "_cooling_cap": 0.0, "_power_rooms": 0,
            "_designed": 0, "_white_racks": 0, "_area": 0.0,
        })
        s["room_count"] += 1
        # SPACE is a white-space question, and so is the rack count beside it.
        # The two cabinets in a plant room hold BMS controllers; counting them
        # as estate capacity would say a site has room to sell that nobody
        # could rack a server into - and a row reading "23 racks" next to a U
        # total drawn from 20 of them is a row that does not add up.
        if r["room_class"] == "white_space":
            s["rack_count"] += r["rack_count"]
            s["_used_u"] += r["_used_u"]
            s["_total_u"] += r["_total_u"]
            s["_designed"] += r["designed_racks"] or 0
            s["_white_racks"] += r["rack_count"]
            s["_area"] += r["floor_area_m2"] or 0.0
        else:
            # Counted, never hidden: someone asking "where are my 44 racks"
            # deserves the four in plant rooms to be findable.
            s["facility_racks"] += r["rack_count"]
        s["_it_kw"] += r["_it_kw"]
        if r["_power_cap"]:
            s["_power_cap"] += r["_power_cap"]
            s["_power_rooms"] += 1
        if r["_cooling_cap"]:
            s["_cooling_cap"] += r["_cooling_cap"]

    out = []
    for s in by_site.values():
        # The site's own design rating beats a sum of room ratings: it is the
        # supply the site was actually built to, and summing rooms would count
        # a shared UPS once per room it feeds.
        site_design = _f(design.get(s["id"]))
        if site_design:
            cap, basis = site_design, "site design IT load"
        elif s["_power_cap"]:
            cap = s["_power_cap"]
            basis = (f"summed nameplate across {s['_power_rooms']} room(s) - "
                     "not de-rated for redundancy")
        else:
            cap, basis = None, "no design rating recorded for this site"
        out.append(_strip({**s,
                           "space_pct": _pct(s["_used_u"], s["_total_u"]),
                           "space_used_u": s["_used_u"], "space_total_u": s["_total_u"],
                           "designed_racks": s["_designed"] or None,
                           "built_out_pct": _pct(s["_white_racks"], s["_designed"] or None),
                           "floor_area_m2": round(s["_area"], 1) or None,
                           "power_pct": _pct(s["_it_kw"], cap),
                           "power_used_kw": round(s["_it_kw"], 1),
                           "power_capacity_kw": None if cap is None else round(cap, 1),
                           "power_basis": basis,
                           "cooling_pct": _pct(s["_it_kw"], s["_cooling_cap"] or None),
                           "cooling_used_kw": round(s["_it_kw"], 1),
                           "cooling_capacity_kw": round(s["_cooling_cap"], 1) or None,
                           "cooling_basis": ("summed rated capacity of the units "
                                             "reporting one" if s["_cooling_cap"]
                                             else "no cooling unit reports a rated capacity")}))
    return sorted(out, key=lambda r: r["site_code"])


def _fold_util_total(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    white = [r for r in rooms if r["room_class"] == "white_space"]
    used_u = sum(r["_used_u"] for r in white)
    total_u = sum(r["_total_u"] for r in white)
    designed = sum(r["designed_racks"] or 0 for r in white)
    # Load stays whole-estate: the plant draws power whoever hides its row.
    it_kw = sum(r["_it_kw"] for r in rooms)
    cool_cap = sum(r["_cooling_cap"] or 0 for r in rooms)
    return {"space_pct": _pct(used_u, total_u),
            "built_out_pct": _pct(sum(r["rack_count"] for r in white), designed or None),
            "designed_racks": designed or None,
            "floor_area_m2": round(sum(r["floor_area_m2"] or 0 for r in white), 1) or None,
            "power_used_kw": round(it_kw, 1),
            "cooling_capacity_kw": round(cool_cap, 1) or None,
            "cooling_pct": _pct(it_kw, cool_cap or None),
            "racks": sum(r["rack_count"] for r in white),
            "rooms": len(white),
            "facility_rooms": len(rooms) - len(white)}


# ---------------------------------------------------------------- alert drills


async def alarms(session: AsyncSession, *, category: str) -> dict[str, Any]:
    """The drill-down behind one category counter, by room.

    `total` is EVERYTHING open in the category, because that is what the
    counter that opens this panel counts; `alarms` is the actionable subset of
    the same number. The two are reported side by side and never added,
    exactly as the row columns do it.

    Every row carries its own severity and detection split. The facets are the
    same population the row totals, computed in the one query that produced it:
    a facet fetched separately can disagree with the row it sits under, and
    then neither number is usable.
    """
    rows = await repo.alarms_by_room(session, category=category)
    unlocated = await repo.unlocated_alarms_by_category(session, category=category)

    def _detect(r: dict[str, Any]) -> dict[str, int]:
        return {d: int(r.get(f"detected_{d}") or 0) for d in DETECTIONS}

    def _sev(r: dict[str, Any]) -> dict[str, int]:
        return {k: int(r.get(k) or 0)
                for k in ("critical", "major", "minor", "warning")}

    out_rows = [{
        "room_id": r["room_id"], "room_name": r["room_name"],
        "floor": r["floor"], "site_id": r["datacenter_id"],
        "site_code": r["site_code"], "site_name": r["site_name"],
        "qty": int(r["qty"]), "devices": int(r["devices"]),
        # The two classes, side by side and never summed into one number here.
        "alerts": int(r.get("alerts") or 0),
        "critical": int(r["critical"]), "major": int(r["major"]),
        "by_severity": _sev(r),
        "by_detection": _detect(r),
    } for r in rows]

    located_alarms = sum(int(r["qty"]) for r in rows)
    located_alerts = sum(int(r.get("alerts") or 0) for r in rows)

    return {
        "category": category,
        "rows": out_rows,
        # What the counter shows: every open condition in this category.
        "total": located_alarms + located_alerts + unlocated["total"],
        # The part of it that needs answering, so the panel can say both
        # without the reader adding a column by eye.
        "alarms": located_alarms + unlocated["alarms"],
        # Platform conditions belong to no room. Reported separately so the
        # panel's rows and the counter that opened it can be reconciled.
        "unlocated": unlocated["total"],
        # Facet totals across the located rows. Unlocated alarms are excluded
        # and said so: they have no room row to face against, and folding them
        # into a facet would make the facets and the rows disagree.
        "by_severity": {
            k: sum(row["by_severity"][k] for row in out_rows)
            for k in ("critical", "major", "minor", "warning")
        },
        "by_detection": {
            d: sum(row["by_detection"][d] for row in out_rows) for d in DETECTIONS
        },
    }


# ------------------------------------------------------------------- room view


async def room_kpi(session: AsyncSession, room_id: str) -> dict[str, Any] | None:
    """The room drawer: what is in it, how warm, how loaded, how full."""
    ident = await repo.room(session, room_id)
    if ident is None:
        return None

    census = await repo.room_census(session, room_id)
    updated = await repo.room_updated(session, room_id)

    th = await thermal(session, mode="live")
    room_thermal = next((r for r in th["rooms"] if r["id"] == room_id), None)

    pw = await power(session, live=True)
    room_power = next((r for r in pw["rooms"] if r["id"] == room_id), None)

    ut = await utilisation(session)
    room_util = next((r for r in ut["rooms"] if r["id"] == room_id), None)

    return {
        "room": ident,
        "is_white_space": ident.get("room_class") == "white_space",
        "monitored": {
            "devices": int(census.get("devices") or 0),
            "online": int(census.get("online") or 0),
            "offline": int(census.get("offline") or 0),
            "racks": (room_util or {}).get("rack_count", 0),
            "cooling_units": int(census.get("cooling_units") or 0),
            "cooling_online": int(census.get("cooling_online") or 0),
            "power_units": int(census.get("power_units") or 0),
            "power_online": int(census.get("power_online") or 0),
        },
        "environmental": {
            "avg_c": (room_thermal or {}).get("avg_c"),
            "max_c": (room_thermal or {}).get("max_c"),
            "compliance_pct": (room_thermal or {}).get("compliance_pct"),
            "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C},
            "note": (room_thermal or {}).get("note"),
            # No humidity instrument exists. Stated here so the drawer can show
            # the field as absent-with-a-reason instead of omitting it and
            # leaving a reader to wonder whether it was simply forgotten.
            "humidity_note": "no humidity instrument in this estate",
        },
        "power": {
            "total_kw": (room_power or {}).get("total_kw"),
            "it_ac_kw": (room_power or {}).get("it_ac_kw"),
            "it_dc_kw": None,
            "cooling_kw": (room_power or {}).get("cooling_kw"),
            "pue": (room_power or {}).get("pue"),
            "note": (room_power or {}).get("note"),
        },
        "utilisation": {
            "space_pct": (room_util or {}).get("space_pct"),
            "power_pct": (room_util or {}).get("power_pct"),
            "power_basis": (room_util or {}).get("power_basis"),
            "cooling_pct": (room_util or {}).get("cooling_pct"),
            "cooling_basis": (room_util or {}).get("cooling_basis"),
        },
        "last_sample": updated,
        "as_of": datetime.now(UTC),
    }
