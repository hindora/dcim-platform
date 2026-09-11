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

from app.core.alert_taxonomy import DETECTIONS, THERMAL_ALARM_CATEGORIES
from app.repositories import estate as repo
from app.repositories import thermal as thermal_repo

# ASHRAE TC 9.9 recommended envelope for class A1-A4 equipment intake air.
# Compliance on this page means "inside the RECOMMENDED band", which is a
# tighter test than the allowable envelope a device will survive - the point of
# the number is efficiency and headroom, not whether anything melted.
BAND_LOW_C = 18.0
BAND_HIGH_C = 27.0

# The ALLOWABLE ceiling (class A1) is where equipment is at risk rather than
# merely inefficient. The page paints warn above the recommended band and
# critical above this one - the same two lines the inlet_temp_high and
# inlet_temp_critical rules (0006) draw, so a row and its alarm agree.
ALLOWABLE_HIGH_C = 32.0

# The other half of the ASHRAE guidance, and the half this page has never
# shown: kit is rated for a maximum RATE of temperature change as well as a
# range, because thermal shock and condensation damage hardware whether or not
# the air ever leaves the band. 20 K/hour is the figure for the equipment
# classes in these halls; a floor with tape would be held to 5.
#
# It is also the operational number. During a cooling loss the absolute
# reading is not the decision - a hall at 26 C climbing 8 K/hour and a hall at
# 26 C holding flat need different answers in the same minute, and they look
# identical without this.
RATE_LIMIT_K_PER_H = 20.0

# Below this, a rate is the sensors talking to themselves. Readings carry
# ~0.3 K of sample noise, which over the fifteen-minute window leaves about
# 1 K/hour of apparent drift - so anything under a couple of K/hour is flat,
# and is shown as flat rather than as a small precise-looking movement.
RATE_NOISE_K_PER_H = 2.0

# The recommended envelope's humidity leg at the intake. Its low end is a
# dew point (-9 C), which no probe in this estate reports, so only the
# ceiling is shown.
RH_HIGH_PCT = 60.0

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
    """Intake temperature, spread and compliance per rack, room and site.

    Everything is derived from the RACK. Intake is a property of a rack - a
    probe on its door, or the servers inside it - so a room's figure is its
    racks' figures added up and a site's is its rooms', with sums and counts
    carried so nothing is ever an average of averages. One payload, one
    instant, three tiers that agree.

    The intake source per rack, in this order:

      1. the rack's environment probes (`ambient_temperature` on a racked
         device - the PDU's probe hung at the front). This is what a DCIM
         calls intake: the cold aisle at the rack face, which is what ASHRAE
         defines, and it survives the servers being decommissioned;
      2. the servers' BMC inlet, where no probe reported. Behind the bezel,
         a degree or two warm from faceplate heating, and blind in a rack
         with no servers - but eighteen of them in a loaded rack are better
         evidence of spread than one probe, which is why the analytics view
         keeps using them for hot spots.

    Exhaust and ΔT come from servers only; nothing else has an exhaust sensor.
    This estate records no probe position, so "front" is inferred from the
    readings (the probes track inlet, not exhaust), not read from inventory.
    """
    if mode == "now":
        # The newest reading from every sensor, and nothing older. A window
        # cannot answer "what is happening": an hour's mean needs an hour to
        # show a step change and holds it for an hour after it clears, so a
        # tripped CRAH read as nothing for ten minutes and as a fault long
        # after it was fixed.
        end = datetime.now(UTC)
        f0, f1 = end - repo.NOW_HORIZON, end
        # The far end of the rate. Not a window: the same newest-per-sensor
        # shot, taken as of fifteen minutes ago, so the two ends are the same
        # measurement of the same sensors and the interval between them is
        # fixed for every row on the page.
        was_at = end - repo.RATE_WINDOW
        c0, c1 = was_at - repo.NOW_HORIZON, was_at
        window = {"mode": "now", "focus_start": f0, "focus_end": f1,
                  "compare_start": c0, "compare_end": c1,
                  "label": "now",
                  "compare_label": f"{int(repo.RATE_WINDOW.total_seconds() // 60)} "
                                   f"minutes ago"}
    elif mode == "live":
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

    # Why a delta is blank, in words. A dot with no reason reads as "the page
    # has no comparison", when the usual truth is that one window had no
    # readings - the fleet was down, or the day had no collection.
    if mode == "now":
        absent_now = "no sensor in this rack has reported in the last ten minutes"
        absent_prev = ("nothing in this rack was reporting fifteen minutes ago, "
                       "so there is no earlier reading to measure against")
    else:
        prep = "in the" if mode == "live" else "on"
        absent_prev = f"no readings {prep} {window['compare_label']}"
        absent_now = f"no readings {prep} {window['label']}"

    skeleton = await repo.thermal_rooms(session)
    if mode == "now":
        raw_racks = await repo.thermal_racks_now(
            session, since=f0, was_at=was_at,
            low_c=BAND_LOW_C, high_c=BAND_HIGH_C,
            allowable_c=ALLOWABLE_HIGH_C)
        p90 = await repo.thermal_p90_now(session, since=f0)
    else:
        raw_racks = await repo.thermal_racks(session, focus_start=f0, focus_end=f1,
                                             compare_start=c0, compare_end=c1,
                                             low_c=BAND_LOW_C, high_c=BAND_HIGH_C,
                                             allowable_c=ALLOWABLE_HIGH_C)
        # A percentile does not fold from sums, so it is taken over the pooled
        # readings at every tier in one query and attached afterwards. Same
        # readings, same source rule, so it agrees with the averages beside it.
        p90 = await repo.thermal_p90(session, focus_start=f0, focus_end=f1)

    silent = ("no sensor in this rack has reported in the last ten minutes"
              if mode == "now" else
              "no intake probe or server sensor in this rack reported in this window")
    # Only the NOW view measures over a span short and fixed enough for a
    # rate to mean anything. See _derive.
    rate_hours = (repo.RATE_WINDOW.total_seconds() / 3600.0
                  if mode == "now" else None)
    racks = [_rack_row(r, absent_now, absent_prev, silent, rate_hours)
             for r in raw_racks]
    rooms = _fold_rooms(skeleton, racks, absent_now, absent_prev, rate_hours)
    sites = _fold_sites(rooms, absent_now, absent_prev, rate_hours)
    totals = _fold_total(rooms, rate_hours)
    alarms_open = await repo.thermal_alarms(
        session, categories=list(THERMAL_ALARM_CATEGORIES))
    await _attach_cooling(session, rooms, sites, totals)
    _attach_count(racks, alarms_open["racks"])
    _attach_count(rooms, alarms_open["rooms"])
    _attach_count(sites, alarms_open["sites"])
    totals["alarms_open"] = alarms_open["total"]
    # How many of a row's conditions are on something IN a rack.
    #
    # A CRAH stands on the floor and belongs to no rack, so a hall can show
    # two open conditions while every rack under it shows none - correct, and
    # unreadable without saying which. Every other figure on this page folds
    # from the racks; this one does not, and the difference is the plant.
    _in_racks: dict[str, int] = {}
    for rack in racks:
        _in_racks[rack["room_id"]] = _in_racks.get(rack["room_id"], 0) + rack["alarms_open"]
    for room in rooms:
        room["alarms_in_racks"] = _in_racks.get(room["id"], 0)
    _by_site: dict[str, int] = {}
    for room in rooms:
        _by_site[room["site_id"]] = _by_site.get(room["site_id"], 0) + room["alarms_in_racks"]
    for site in sites:
        site["alarms_in_racks"] = _by_site.get(site["id"], 0)
    totals["alarms_in_racks"] = sum(r["alarms_in_racks"] for r in rooms)
    _attach_p90(racks, p90["racks"])
    _attach_p90(rooms, p90["rooms"])
    _attach_p90(sites, p90["sites"])
    totals["p90_c"] = (round(p90["total"], 1)
                       if p90["total"] is not None and totals["samples"] else None)
    return {
        "window": window,
        "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
                 "allowable_high_c": ALLOWABLE_HIGH_C,
                 "rh_high_pct": RH_HIGH_PCT,
                 # The rate half of the same guidance, and the window the NOW
                 # view measures it over. Null in the other modes, where the
                 # comparison is too long to be a rate.
                 "rate_limit_k_per_h": RATE_LIMIT_K_PER_H,
                 "rate_noise_k_per_h": RATE_NOISE_K_PER_H,
                 "rate_window_minutes": (
                     int(repo.RATE_WINDOW.total_seconds() // 60)
                     if mode == "now" else None),
                 "basis": "ASHRAE TC 9.9 recommended envelope for intake air"},
        # Echoed so the page opens its drill-down with the same list the
        # counts were taken over, rather than a copy that can drift.
        "alarm_categories": list(THERMAL_ALARM_CATEGORIES),
        "totals": totals,
        "sites": sites,
        "rooms": [_strip(r) for r in rooms],
        "racks": [_strip(r) for r in racks],
        "notes": [_source_note(totals), _humidity_note(totals),
                  _distribution_note(), *_window_note(mode)],
    }


# The private keys every tier carries so the tier above can fold it. Sums and
# counts, never averages; maxima; and how many racks each source spoke for.
_ACC_SUMS = ("_sum", "_n", "_in_band", "_below", "_hot", "_prev_sum", "_prev_n",
             "_rh_sum", "_rh_n", "_rh_probes", "_probes", "_servers", "_network")
_ACC_MAXES = ("_max", "_prev_max", "_rh_max")


async def _attach_cooling(session: AsyncSession, rooms: list[dict[str, Any]],
                          sites: list[dict[str, Any]],
                          totals: dict[str, Any]) -> None:
    """Hang each room's cooling units on its row, and fold the counts upward.

    Everything else on this page is racks added up. This is the floor plant,
    which belongs to no rack - and until now it was only visible after drilling
    into a room, so the page's own thesis, that a high SUPPLY and a high RETURN
    send an engineer to opposite ends of the building, was invisible at the
    level where an operator starts.

    The verdicts and the summary come from the thermal service, the same
    function the room view uses, so a hall cannot be described one way here and
    another way one click deeper.
    """
    from app.services import thermal as thermal_svc

    by_room = await thermal_repo.crahs_by_room(session)
    per_room: dict[str, dict[str, Any]] = {}
    for room_id, rows in by_room.items():
        units = [
            thermal_svc.CrahThermal(
                device_id=r["device_id"], name=r["name"],
                supply_c=_f(r["supply_c"]), return_c=_f(r["return_c"]),
                setpoint_c=_f(r["setpoint_c"]),
                running=None if r["running"] is None else bool(r["running"]),
            )
            for r in rows
        ]
        # The room's own baseline, as the room view builds it: what counts as a
        # high return depends on the hall, not on a number chosen here.
        p90 = thermal_svc.percentile([u.return_c for u in units if u.return_c], 90)
        per_room[room_id] = thermal_svc.room_cooling(units, p90)

    for row in rooms:
        row["cooling"] = per_room.get(row["id"])

    def fold(rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
        """Counts add; temperatures do not.

        A mean of two halls' supply air describes neither of them, and a delta
        folded across rooms is a number about nowhere. So a site carries how
        many units it has and how many are misbehaving, and the temperatures
        stay where they were measured.
        """
        found = [r["cooling"] for r in rows if r.get("cooling")]
        if not found:
            return None
        return {
            "units": sum(c["units"] for c in found),
            "units_stopped": sum(c["units_stopped"] for c in found),
            "units_high_supply": sum(c["units_high_supply"] for c in found),
            "units_high_return": sum(c["units_high_return"] for c in found),
            "supply_c": None, "return_c": None, "delta_t_k": None,
        }

    for site in sites:
        site["cooling"] = fold([r for r in rooms if r["site_id"] == site["id"]],
                               site["id"])
    totals["cooling"] = fold(rooms, "estate")


def _rack_row(r: dict[str, Any], absent_now: str, absent_prev: str,
              absent: str, rate_hours: float | None = None) -> dict[str, Any]:
    p_n = int(r.get("p_n") or 0)
    f_n = int(r.get("f_n") or 0)
    if p_n:
        source = "probes"
        n, s_sum, mx, in_band = p_n, r["p_sum"], r["p_max"], r["p_in_band"]
        below, hot = r.get("p_below"), r.get("p_hot")
        sensors = int(r.get("p_sensors") or 0)
        prev_n, prev_sum, prev_max = int(r.get("pc_n") or 0), r.get("pc_sum"), r.get("pc_max")
    elif f_n:
        source = "servers"
        n, s_sum, mx, in_band = f_n, r["f_sum"], r["f_max"], r["f_in_band"]
        below, hot = r.get("f_below"), r.get("f_hot")
        sensors = int(r.get("f_sensors") or 0)
        prev_n, prev_sum, prev_max = int(r.get("c_n") or 0), r.get("c_sum"), r.get("c_max")
    elif int(r.get("n_n") or 0):
        # Last resort: the front-panel sensor on the switches. A spine or
        # management rack holds no servers and often no probe, and it used to
        # read as a dash - no temperature at all - while its switches were
        # publishing this the whole time. Behind the bezel, so it runs a
        # degree or two above the air the rack is breathing, which is why it
        # is ranked under both real intake sources and named on the row.
        source = "network"
        n, s_sum, mx = int(r["n_n"]), r["n_sum"], r["n_max"]
        in_band = r["n_in_band"]
        below, hot = r.get("n_below"), r.get("n_hot")
        sensors = int(r.get("n_sensors") or 0)
        prev_n, prev_sum, prev_max = int(r.get("nc_n") or 0), r.get("nc_sum"), r.get("nc_max")
    else:
        source, n, s_sum, mx, in_band, sensors = None, 0, 0.0, None, 0, 0
        below = hot = 0
        # Nothing this window; whichever source spoke last window names the
        # comparison for the note, and the delta is None regardless.
        prev_n = int(r.get("pc_n") or r.get("c_n") or r.get("nc_n") or 0)
        prev_sum, prev_max = None, None
    e_n = int(r.get("e_n") or 0)
    rh_n = int(r.get("rh_n") or 0)

    row = {
        "id": r["rack_id"],
        "kind": "rack",
        "name": r["rack_name"],
        "row": r.get("row_name"),
        "u_height": r.get("u_height"),
        "room_id": r["room_id"],
        "room_name": r["room_name"],
        "floor": r.get("floor"),
        "room_class": r.get("room_class"),
        "site_id": r["datacenter_id"],
        "site_code": r["site_code"],
        "site_name": r["site_name"],
        "source": source,
        "sensors": sensors,
        "exhaust_c": round(float(r["e_sum"]) / e_n, 1) if e_n else None,
        "_sum": float(s_sum or 0.0), "_n": n, "_in_band": int(in_band or 0),
        "_below": int(below or 0), "_hot": int(hot or 0),
        "_prev_sum": _f(prev_sum) or 0.0, "_prev_n": prev_n,
        "_max": _f(mx), "_prev_max": _f(prev_max),
        "_rh_sum": _f(r.get("rh_sum")) or 0.0, "_rh_n": rh_n,
        "_rh_max": _f(r.get("rh_max")), "_rh_probes": int(r.get("rh_probes") or 0),
        "_probes": 1 if source == "probes" else 0,
        "_servers": 1 if source == "servers" else 0,
        "_network": 1 if source == "network" else 0,
    }
    row.update(_derive(row, absent_now, absent_prev, absent=absent,
                       rate_hours=rate_hours))
    # Exhaust minus intake: the heat the air actually carried away. Low on a
    # loaded rack is bypass air, not a cooling shortage.
    row["delta_t_k"] = (round(row["exhaust_c"] - row["avg_c"], 1)
                        if row["exhaust_c"] is not None and row["avg_c"] is not None
                        else None)
    return row


def _derive(acc: dict[str, Any], absent_now: str, absent_prev: str, *,
            absent: str, rate_hours: float | None = None) -> dict[str, Any]:
    """The public figures from the private sums, identical at every tier.

    `rate_hours` is the span the comparison covers, and only the caller knows
    it: fifteen minutes in the NOW view, a whole day or a whole hour in the
    others. Without one there is no rate, which is the honest answer for a
    comparison between two calendar days - the change is real, the rate is a
    fiction spread over 24 hours of unequal weather and load.
    """
    n, prev_n, rh_n = acc["_n"], acc["_prev_n"], acc["_rh_n"]
    avg = round(acc["_sum"] / n, 1) if n else None
    prev_avg = round(acc["_prev_sum"] / prev_n, 1) if prev_n else None
    mx = None if acc["_max"] is None else round(acc["_max"], 1)
    prev_mx = None if acc["_prev_max"] is None else round(acc["_prev_max"], 1)
    return {
        "avg_c": avg,
        "max_c": mx,
        "compliance_pct": _pct(acc["_in_band"], n) if n else None,
        "below_pct": _pct(acc["_below"], n) if n else None,
        "distribution": _distribution(acc),
        "samples": n,
        "delta_avg": _delta(avg, prev_avg),
        "delta_max": _delta(mx, prev_mx),
        "delta_note": _delta_note(n, prev_n, absent_now, absent_prev),
        # Per HOUR, whatever window it was measured over, because that is the
        # unit the ASHRAE limit is written in and the unit an operator
        # estimates time-to-trouble with. A change of 2 K over fifteen minutes
        # is 8 K/hour, and 8 K/hour on a hall at 26 C is a different situation
        # from 26 C holding still.
        "rate_k_per_h": _rate(avg, prev_avg, rate_hours),
        # Humidity rides beside compliance, not inside it: folding RH into the
        # in-band share would silently change what that number has meant.
        "rh_avg": round(acc["_rh_sum"] / rh_n, 1) if rh_n else None,
        "rh_max": None if acc["_rh_max"] is None else round(acc["_rh_max"], 1),
        "rh_probes": acc["_rh_probes"],
        "sources": {"probes": acc["_probes"], "servers": acc["_servers"],
                    "network": acc["_network"]},
        # Said once per row, so a reader never has to guess whether a blank
        # cell means "cool" or "nobody is measuring".
        "note": None if n else absent,
    }


def _rate(now_v: float | None, was_v: float | None,
          hours: float | None) -> float | None:
    """Change per hour between two readings of the same sensors.

    None where either end is missing or the caller has no interval - a rate
    over an unknown span is not a rate, and the modes that compare a day with
    another day have nothing to divide by that would mean anything.
    """
    if now_v is None or was_v is None or not hours:
        return None
    return round((now_v - was_v) / hours, 1)


def _distribution(acc: dict[str, Any]) -> dict[str, float] | None:
    """Where the readings fell against the ASHRAE lines, as shares of the row.

    Four bands that partition every reading: below the recommended floor
    (overcooled - the finding a floor most often pays for), inside the
    recommended band, above it but inside the allowable envelope, and above
    the allowable ceiling. Counts fold by addition, so a room's split is its
    racks' readings pooled, never an average of percentages.
    """
    n = acc["_n"]
    if not n:
        return None
    below, in_band, hot = acc["_below"], acc["_in_band"], acc["_hot"]
    above_rec = max(n - below - in_band - hot, 0)
    return {
        "below_pct": _pct(below, n),
        "in_band_pct": _pct(in_band, n),
        "above_recommended_pct": _pct(above_rec, n),
        "above_allowable_pct": _pct(hot, n),
    }


def _attach_count(rows: list[dict[str, Any]], by_id: dict[str, int]) -> None:
    """Zero, not absent: a row with nothing open is a fact, and a dash there
    would read as "not counted"."""
    for r in rows:
        r["alarms_open"] = int(by_id.get(r["id"], 0))
        # A rack has nothing under it, so everything it counts is its own.
        r.setdefault("alarms_in_racks", r["alarms_open"])


def _attach_p90(rows: list[dict[str, Any]], by_id: dict[str, float | None]) -> None:
    """p90 rides on the row whose readings it was taken over; None elsewhere."""
    for r in rows:
        v = by_id.get(r["id"]) if r["samples"] else None
        r["p90_c"] = None if v is None else round(v, 1)


def _empty_acc() -> dict[str, Any]:
    acc: dict[str, Any] = dict.fromkeys(_ACC_SUMS, 0)
    acc["_sum"] = acc["_prev_sum"] = acc["_rh_sum"] = 0.0
    for k in _ACC_MAXES:
        acc[k] = None
    return acc


def _add(acc: dict[str, Any], row: dict[str, Any]) -> None:
    for k in _ACC_SUMS:
        acc[k] += row[k]
    for k in _ACC_MAXES:
        if row[k] is not None:
            acc[k] = row[k] if acc[k] is None else max(acc[k], row[k])


def _fold_rooms(skeleton: list[dict[str, Any]], racks: list[dict[str, Any]],
                absent_now: str, absent_prev: str,
                rate_hours: float | None = None) -> list[dict[str, Any]]:
    by_room: dict[str, dict[str, Any]] = {}
    for rack in racks:
        _add(by_room.setdefault(rack["room_id"], _empty_acc()), rack)
    out = []
    for r in skeleton:
        acc = by_room.get(r["room_id"], _empty_acc())
        out.append({
            "id": r["room_id"], "kind": "room", "name": r["room_name"],
            "floor": r["floor"], "room_type": r["room_type"],
            "room_class": r["room_class"], "site_id": r["datacenter_id"],
            "site_code": r["site_code"], "site_name": r["site_name"],
            "rack_count": int(r["rack_count"] or 0),
            **acc,
            **_derive(acc, absent_now, absent_prev, rate_hours=rate_hours,
                      absent="no rack intake sensor reported in this window"),
        })
    return out


def _fold_sites(rooms: list[dict[str, Any]], absent_now: str,
                absent_prev: str,
                rate_hours: float | None = None) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for r in rooms:
        s = by_site.setdefault(r["site_id"], {
            "id": r["site_id"], "kind": "site", "name": r["site_name"],
            "site_id": r["site_id"], "site_code": r["site_code"],
            "site_name": r["site_name"], "room_count": 0, "rack_count": 0,
            **_empty_acc(),
        })
        s["room_count"] += 1
        s["rack_count"] += r["rack_count"]
        _add(s, r)
    out = [_strip({**s, **_derive(s, absent_now, absent_prev,
                                  rate_hours=rate_hours,
                                  absent="no rack intake sensor reported")})
           for s in by_site.values()]
    return sorted(out, key=lambda r: r["site_code"])


def _fold_total(rooms: list[dict[str, Any]],
                rate_hours: float | None = None) -> dict[str, Any]:
    acc = _empty_acc()
    for r in rooms:
        _add(acc, r)
    n, rh_n = acc["_n"], acc["_rh_n"]
    prev_n = acc["_prev_n"]
    white = [r for r in rooms if r["room_class"] == "white_space"]
    return {
        "avg_c": round(acc["_sum"] / n, 1) if n else None,
        "max_c": None if acc["_max"] is None else round(acc["_max"], 1),
        "compliance_pct": _pct(acc["_in_band"], n) if n else None,
        "below_pct": _pct(acc["_below"], n) if n else None,
        "distribution": _distribution(acc),
        "samples": n,
        # The estate is going somewhere too, and the headline band is where
        # that is read first. Same arithmetic as every tier below it, so the
        # figure at the top is the rows added up rather than a second opinion.
        "rate_k_per_h": _rate(
            round(acc["_sum"] / n, 1) if n else None,
            round(acc["_prev_sum"] / prev_n, 1) if prev_n else None,
            rate_hours),
        "rh_avg": round(acc["_rh_sum"] / rh_n, 1) if rh_n else None,
        "rh_max": None if acc["_rh_max"] is None else round(acc["_rh_max"], 1),
        "rh_probes": acc["_rh_probes"],
        "sources": {"probes": acc["_probes"], "servers": acc["_servers"],
                    "network": acc["_network"]},
        # Reporting is counted over WHITE SPACE only. Rack intake sensors exist
        # where racks do; counting a generator room as a room that failed to
        # report made the ratio read as a fleet of dead sensors.
        "rooms_reporting": sum(1 for r in white if r["_n"]),
        "rooms": len(white),
        "facility_rooms": len(rooms) - len(white),
    }


def _delta_note(n: int, prev_n: int, absent_now: str, absent_prev: str) -> str | None:
    """None when both windows have readings and the delta stands on its own."""
    if not n:
        return absent_now
    if not prev_n:
        return absent_prev
    return None


def _source_note(totals: dict[str, Any]) -> str:
    probes = int(totals["sources"]["probes"])
    servers = int(totals["sources"]["servers"])
    network = int(totals["sources"].get("network") or 0)

    def _racks(n: int) -> str:
        return f"{n} rack{'s' if n != 1 else ''}"

    return (f"Rack intake is the rack's front environment probe where one reported "
            f"({_racks(probes)}), else the servers' BMC inlet ({_racks(servers)}), "
            f"else the front-panel sensor on the rack's network gear "
            f"({_racks(network)}) - which sits behind the bezel and reads a degree "
            "or two warm, but is what a rack of switches has instead of nothing. "
            "Exhaust and ΔT come from servers only. This estate records no probe "
            "position; front is inferred from the readings, which track inlet "
            "rather than exhaust.")


def _window_note(mode: str) -> list[str]:
    """What a row counts, when that changes what the figures mean."""
    if mode != "now":
        return []
    mins = int(repo.RATE_WINDOW.total_seconds() // 60)
    return ["Now is the newest reading from each sensor, and nothing older than "
            "ten minutes. Every figure counts one reading per SENSOR rather than "
            "every reading over a window, so the in-band share is the share of "
            "sensors in band at this instant. Use it while something is "
            "happening: an hour's mean needs an hour to show a change and holds "
            "it for an hour after it clears.",
            f"Rate is the same shot taken again as of {mins} minutes ago, "
            f"divided out to K/hour - the unit ASHRAE writes its "
            f"{RATE_LIMIT_K_PER_H:g} K/hour limit on rate of change in, and the "
            "unit an operator estimates time-to-trouble with. Fixed for every "
            "row, so a rack read by a 120 s probe and one read by 60 s BMCs are "
            f"the same measurement. Under {RATE_NOISE_K_PER_H:g} K/hour is "
            "sensor noise rather than air, and reads as flat. A rack with "
            f"nothing reporting {mins} minutes ago has no rate rather than a "
            "large one."]


def _distribution_note() -> str:
    return (f"Spread splits the same intake readings by the ASHRAE lines: below "
            f"{BAND_LOW_C:g} °C is overcooled, the most common finding on a real "
            f"floor and the evidence for raising a setpoint; {BAND_LOW_C:g}-"
            f"{BAND_HIGH_C:g} °C recommended; {BAND_HIGH_C:g}-{ALLOWABLE_HIGH_C:g} °C "
            f"allowable; above {ALLOWABLE_HIGH_C:g} °C at risk. p90 is the 90th "
            "percentile of the row's pooled readings, interpolated, over the focus "
            "window only: what the row runs at without one sensor's spike deciding, "
            "which Max lets happen.")


def _humidity_note(totals: dict[str, Any]) -> str:
    probes = int(totals.get("rh_probes") or 0)
    if not probes:
        return ("Relative humidity is not shown: no rack humidity probe reported "
                "in this window, and deriving one from dry and wet bulb would "
                "publish a calculation as a reading.")
    return (f"Relative humidity comes from {probes} rack PDU environment probes "
            "at the intake. CRAH humidity sensors are excluded: they read the "
            "return air, which is the room's exhaust. RH sits beside the in-band "
            "figure and is not folded into it.")


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


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


async def alarms(session: AsyncSession, *,
                 categories: list[str],
                 lifecycle: str = "open",
                 severities: list[str] | None = None,
                 detections: list[str] | None = None) -> dict[str, Any]:
    """The drill-down behind one counter, by room.

    `lifecycle` is `open` for the counter's own population and `all` for the
    history behind it: the same rows, the same arithmetic, with the cleared
    conditions back in. Every figure below is computed from whichever
    population was asked for, so a history panel's headline adds up to its
    rows the way a live one's does.

    Takes the counter's whole set of categories - Cooling is two of them - so
    one room is one row whatever opened the panel.

    `total` is EVERYTHING open in the category, because that is what the
    counter that opens this panel counts; `alarms` is the actionable subset of
    the same number. The two are reported side by side and never added,
    exactly as the row columns do it.

    Every row carries its own severity and detection split. The facets are the
    same population the row totals, computed in the one query that produced it:
    a facet fetched separately can disagree with the row it sits under, and
    then neither number is usable.
    """
    rows = await repo.alarms_by_room(session, categories=categories,
                                     lifecycle=lifecycle,
                                     severities=severities, detections=detections)
    unlocated = await repo.unlocated_alarms_by_category(
        session, categories=categories, lifecycle=lifecycle,
        severities=severities, detections=detections)

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
        # Devices with anything open, for the panels that list both classes.
        "devices_all": int(r.get("devices_all") or 0),
        # The two classes, side by side and never summed into one number here.
        "alerts": int(r.get("alerts") or 0),
        "critical": int(r["critical"]), "major": int(r["major"]),
        "by_severity": _sev(r),
        "by_detection": _detect(r),
    } for r in rows]

    located_alarms = sum(int(r["qty"]) for r in rows)
    located_alerts = sum(int(r.get("alerts") or 0) for r in rows)

    return {
        "categories": categories,
        "lifecycle": lifecycle,
        # Echoed so a caller holding two responses can tell which is which.
        "severities": [s.upper() for s in (severities or [])],
        "detections": list(detections or []),
        "rows": out_rows,
        # What the counter shows: every open condition in this category, in a
        # room. Platform conditions are NOT in here and are not in the counter
        # that opened this panel either - location decides which of the two
        # populations a condition belongs to, so the rows add up to the
        # headline and the headline adds up to the strip.
        "total": located_alarms + located_alerts,
        # The part of it that needs answering, so the panel can say both
        # without the reader adding a column by eye.
        "alarms": located_alarms,
        # Reported so the panel can name what it is NOT counting and point at
        # the badge that is. Split by class for the same reason the rows are.
        "unlocated": unlocated["total"],
        "unlocated_alarms": unlocated["alarms"],
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


MAX_TREND_DAYS = 366


async def alarm_trend(session: AsyncSession, *, categories: list[str],
                      days: int = 30, bucket: str = "day",
                      since: date | None = None, until: date | None = None,
                      room_id: str | None = None,
                      datacenter_id: str | None = None) -> dict[str, Any]:
    """Conditions raised per bucket over a window, in one scope.

    The window is the last `days` ending today, or - when `since` and
    `until` are both given - exactly those dates, inclusive. A picked range
    is the operator's question ("what happened over the change freeze"),
    so it is honoured to the day and never widened to a preset; only the
    weekly bucket moves its start back to a Monday, as it does for presets.

    The table beside it says what is happening; this says whether that is
    normal - a hall raising six a day for a month and a hall that started
    this morning are different problems wearing the same count.

    Every bucket in the window is present, zero included, oldest first: the
    chart draws the axis from these and a missing one would shift every bar
    after it one column left.

    Weekly buckets are Monday-anchored, so the window is widened back to the
    Monday on or before its first day: a first week counted from Wednesday
    would be a short bar that looks like a quiet one.
    """
    if (since is None) != (until is None):
        raise ValueError("since and until go together")
    if since is not None and until is not None:
        if until < since:
            raise ValueError("until is before since")
        if (until - since).days + 1 > MAX_TREND_DAYS:
            raise ValueError(f"a window is at most {MAX_TREND_DAYS} days")
        last = until
        first = since
        days = (until - since).days + 1
    else:
        last = datetime.now(UTC).date()
        first = last - timedelta(days=days - 1)
    step = 7 if bucket == "week" else 1
    if bucket == "week":
        first -= timedelta(days=first.weekday())
    since_at = datetime.combine(first, time.min, tzinfo=UTC)
    # Exclusive end: the instant after the last day, so the last day's
    # conditions are inside the window and tomorrow's are not.
    until_at = datetime.combine(last + timedelta(days=1), time.min, tzinfo=UTC)
    rows = await repo.alarm_trend(session, categories=categories,
                                  since=since_at, until=until_at,
                                  bucket=bucket, room_id=room_id,
                                  datacenter_id=datacenter_id)
    raised = {str(r["day"]): int(r["n"]) for r in rows}
    starts: list[date] = []
    d = first
    while d <= last:
        starts.append(d)
        d += timedelta(days=step)
    points = [{"day": d.isoformat(), "raised": raised.get(d.isoformat(), 0)}
              for d in starts]
    return {
        "categories": categories,
        "days": days,
        "bucket": bucket,
        "since": since_at,
        "until": last,
        "room_id": room_id,
        "datacenter_id": datacenter_id,
        "points": points,
        "total": sum(p["raised"] for p in points),
    }


TREND_MAX_DAYS = 366
_TREND_WIDTH = {"hour": timedelta(hours=1), "day": timedelta(days=1)}
# A day of hourly points reads the five-minute rollup, so each hour's p90 has
# twelve values per sensor behind it; anything longer reads the hourly
# rollup, whose chunks are a twelfth the size - a week of five-minute rows
# from the uncompressed newest chunk is a ten-second cold read.
_FINE_SOURCE_UP_TO = timedelta(days=2)
#: How much of the newest hourly points to redraw from the hypertable.
#:
#: A rollup is never more current than its refresh policy: the five-minute
#: aggregate lands up to ten minutes behind and the hourly one up to ninety.
#: During an incident that is the bucket somebody is watching, so the tail is
#: read from the raw table, which is current to the last poll. Two hours is
#: enough to cover the hourly policy's lag and cheap enough to run every
#: refresh - the same scan the live table does.
_RAW_TAIL = timedelta(hours=2)
# The hourly rollup closes an hour late and refreshes every half hour, so
# its newest one to two buckets are empty while the five-minute one already
# has them. For hourly points read from it, the tail of the window is
# re-read from the five-minute rollup and fills only the buckets the hourly
# one lacked - the line then ends at the current hour, not two hours ago.
_TAIL_FROM_FINE = timedelta(hours=3)


async def thermal_trend(session: AsyncSession, *, days: int = 7,
                        bucket: str = "hour", since: date | None = None,
                        until: date | None = None, room_id: str | None = None,
                        datacenter_id: str | None = None,
                        rack_id: str | None = None) -> dict[str, Any]:
    """Intake average, p90 and max per hour or per day, in one scope.

    The table's Δ columns compare two windows and nothing more; this is the
    run-up. Same readings, same probe-first source rule, drawn against the
    ASHRAE band so "warm" has a shape - a hall drifting up over a week and a
    hall that spiked this morning are different problems wearing the same
    Max.

    The window is the last `days` (hourly: ending at the top of the next
    hour, so the bucket in progress is drawn; daily: UTC days ending today),
    or exactly `since`..`until` inclusive when both are given. Every bucket
    in the window is present, oldest first; one nothing reported in carries
    nulls, so the line breaks there instead of bridging a period nobody
    measured.
    """
    if bucket not in _TREND_WIDTH:
        raise ValueError("bucket is hour or day")
    width = _TREND_WIDTH[bucket]
    if (since is None) != (until is None):
        raise ValueError("since and until go together")
    if since is not None and until is not None:
        if until < since:
            raise ValueError("until is before since")
        if (until - since).days + 1 > TREND_MAX_DAYS:
            raise ValueError(f"a window is at most {TREND_MAX_DAYS} days")
        start = datetime.combine(since, time.min, tzinfo=UTC)
        end = datetime.combine(until + timedelta(days=1), time.min, tzinfo=UTC)
        days = (until - since).days + 1
    else:
        if not 1 <= days <= TREND_MAX_DAYS:
            raise ValueError(f"days is 1 to {TREND_MAX_DAYS}")
        now = datetime.now(UTC)
        if bucket == "hour":
            end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            end = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
        start = end - timedelta(days=days)

    source = "5m" if bucket == "hour" and end - start <= _FINE_SOURCE_UP_TO else "1h"
    rows = await repo.thermal_trend(session, start=start, end=end, bucket=bucket,
                                    source=source, room_id=room_id,
                                    datacenter_id=datacenter_id, rack_id=rack_id)
    by = {r["b"]: r for r in rows}
    if source == "1h" and bucket == "hour":
        tail = await repo.thermal_trend(session, start=max(start, end - _TAIL_FROM_FINE),
                                        end=end, bucket=bucket, source="5m",
                                        room_id=room_id, datacenter_id=datacenter_id,
                                        rack_id=rack_id)
        for r in tail:
            by.setdefault(r["b"], r)
    if bucket == "hour":
        # The newest buckets REPLACE what a rollup said about them rather than
        # filling gaps: the rollup's version of the current hour is not
        # missing, it is a few minutes short, and a partial bucket drawn as a
        # finished one is what made a fault look like it had not arrived.
        fresh = await repo.thermal_trend(session, start=max(start, end - _RAW_TAIL),
                                         end=end, bucket=bucket, source="raw",
                                         room_id=room_id, datacenter_id=datacenter_id,
                                         rack_id=rack_id)
        for r in fresh:
            by[r["b"]] = r
    points = []
    t = start
    while t < end:
        r = by.get(t)
        points.append({
            "t": t,
            "avg_c": None if r is None or r["avg_c"] is None else round(float(r["avg_c"]), 1),
            "p90_c": None if r is None or r["p90_c"] is None else round(float(r["p90_c"]), 1),
            "max_c": None if r is None or r["max_c"] is None else round(float(r["max_c"]), 1),
            "sensors": int(r["sensors"]) if r is not None else 0,
        })
        t += width
    return {
        "days": days,
        "bucket": bucket,
        # Which rollup the points came from: what one value per sensor per
        # sub-bucket means for the p90.
        "source": source,
        "since": start,
        # Exclusive: the instant after the last bucket.
        "until": end,
        "room_id": room_id,
        "datacenter_id": datacenter_id,
        "rack_id": rack_id,
        "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
                 "allowable_high_c": ALLOWABLE_HIGH_C},
        "points": points,
        "buckets_with_data": sum(1 for p in points if p["avg_c"] is not None),
        # The most sensors any one bucket heard from, so the caption can say
        # how much of the scope the line speaks for.
        "sensors": max((p["sensors"] for p in points), default=0),
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
            "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
                     "allowable_high_c": ALLOWABLE_HIGH_C,
                     "rh_high_pct": RH_HIGH_PCT},
            "note": (room_thermal or {}).get("note"),
            # From the rack PDU environment probes, same source as the estate
            # page. Absent-with-a-reason when this room has none, so a reader
            # never wonders whether the field was simply forgotten.
            "rh_avg": (room_thermal or {}).get("rh_avg"),
            "rh_max": (room_thermal or {}).get("rh_max"),
            "rh_probes": int((room_thermal or {}).get("rh_probes") or 0),
            "humidity_note": (
                f"{(room_thermal or {}).get('rh_probes')} rack probes, "
                f"max {(room_thermal or {}).get('rh_max')} %"
                if (room_thermal or {}).get("rh_probes")
                else "no rack humidity probe reported in the last hour"),
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
