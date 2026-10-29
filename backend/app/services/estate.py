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

from app.core.alert_taxonomy import (
    DETECTIONS,
    POWER_ALARM_CATEGORIES,
    POWER_BANDS,
    THERMAL_ALARM_CATEGORIES,
)
from app.core.ashrae import DEFAULT_CLASS, envelope_for
from app.repositories import estate as repo
from app.repositories import thermal as thermal_repo
from app.services import plant as plant_service

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

#: The intake sources a reader may pin the page to, and the metric each one is.
#:
#: Normally the platform picks per rack - probe, else servers, else the
#: switches' front panels - because one of them is simply the better answer
#: and letting somebody choose the worse one is not a feature. Pinning exists
#: for the job the automatic rule cannot do: reading the same hall twice,
#: once by probe and once by BMC, to see whether a probe has drifted.
#:
#: There is no "both". Averaging two sources produces a figure that is
#: neither, weighted by whichever happens to have more sensors in that rack.
SOURCE_METRIC = {
    "probes": "ambient_temperature",
    "servers": "inlet_temperature",
    "network": "network_intake",
}

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


def _attach_envelope(racks: list[dict[str, Any]], rooms: list[dict[str, Any]],
                     sites: list[dict[str, Any]], totals: dict[str, Any],
                     by_rack: dict[str, dict[str, Any]],
                     skeleton: list[dict[str, Any]]) -> None:
    """Hang the three legs on every tier, folding from the racks as everything
    else on this page does - except moisture, which belongs to the room.

    Temperature and rate are per sensor and arrive as SUMS with a sensor
    COUNT, so a room is the sum of its racks over the count of its racks'
    sensors, never a mean of rack means.

    MOISTURE IS ROOM-SCOPED. Air in a hall mixes and humidity barely varies
    across it, which is why no one fits an element to every rack and why
    ASHRAE writes moisture as a condition of the space. The room's own probes
    decide its state and every sensor in the room inherits it; a rack reports
    its room's figure rather than one of its own.

    Scored per rack instead - which this did first - the failure vanished. A
    hall of 44 sensors with 6 probes averaged a real 99.4 % moisture reading
    against 38 sensors that cannot measure moisture and therefore "passed",
    producing a 99.96 % envelope that prints as 100.0, and the estate fold
    diluted it again. A leg no sensor can measure must never be able to vote
    that the leg passed.

    Above the room, moisture is weighted by the SENSORS THE VERDICT WAS
    APPLIED TO - the room's whole population, not its probe count - because
    that is what the envelope figure beside it is weighted by, and two shares
    on one row that divide by different things cannot be read together.
    """
    per_sensor_legs = ("temp", "rate", "env")

    def _blank() -> dict[str, Any]:
        return {f"{k}_sum": 0.0 for k in per_sensor_legs} | {
            "sensors": 0.0, "rate_sensors": 0.0, "moisture_graded": 0.0,
            "peak_rate": None,
            # Room moisture, carried up as share x sensors so it folds like
            # everything else.
            "moist_weighted": 0.0, "moist_sensors": 0.0, "probes": 0.0}

    def _add(acc: dict[str, Any], src: dict[str, Any]) -> None:
        for k in per_sensor_legs:
            v = src.get(f"{k}_sum")
            if v is not None:
                acc[f"{k}_sum"] = (acc.get(f"{k}_sum") or 0.0) + float(v)
        for k in ("sensors", "rate_sensors", "moisture_graded"):
            acc[k] = acc.get(k, 0.0) + float(src.get(k) or 0.0)
        peak = src.get("peak_rate")
        if peak is not None:
            acc["peak_rate"] = max(acc.get("peak_rate") or 0.0, float(peak))

    def _emit(row: dict[str, Any], acc: dict[str, Any],
              moisture_pct: float | None, probes: int) -> None:
        sensors = acc.get("sensors", 0.0)
        row["envelope"] = {
            "temp_pct": _leg_pct(acc.get("temp_sum"), sensors),
            "moisture_pct": moisture_pct,
            "rate_pct": _leg_pct(acc.get("rate_sum"), acc.get("rate_sensors", 0.0)),
            "envelope_pct": _leg_pct(acc.get("env_sum"), sensors),
            "sensors": int(sensors),
            # Probes that spoke for the moisture verdict, and how many sensors
            # it was applied to. A room with no probe has neither.
            "moisture_probes": int(probes),
            "moisture_sensors": int(acc.get("moisture_graded", 0.0)),
            "rate_sensors": int(acc.get("rate_sensors", 0.0)),
            "peak_rate_k_per_h": (round(acc["peak_rate"], 1)
                                  if acc.get("peak_rate") is not None else None),
        }

    # Room moisture arrives repeated on every rack row of that room; take it
    # once.
    room_moist: dict[str, tuple[float | None, int]] = {}
    for hit in by_rack.values():
        rid = str(hit.get("room_id") or "")
        share = hit.get("room_moist_ok")
        room_moist.setdefault(
            rid, (None if share is None else round(float(share) * 100.0, 1),
                  int(hit.get("room_probes") or 0)))

    room_acc: dict[str, dict[str, Any]] = {}
    for rack in racks:
        acc = _blank()
        hit = by_rack.get(str(rack["id"]))
        if hit:
            _add(acc, hit)
        moist, probes = room_moist.get(rack["room_id"], (None, 0))
        _emit(rack, acc, moist, probes)
        room = room_acc.setdefault(rack["room_id"], _blank())
        if hit:
            _add(room, hit)

    site_acc: dict[str, dict[str, Any]] = {}
    total_acc = _blank()
    classes = {r["room_id"]: (r.get("ashrae_class"), r.get("max_rate_k_per_h"))
               for r in skeleton}
    for room in rooms:
        acc = room_acc.get(room["id"], _blank())
        moist, probes = room_moist.get(room["id"], (None, 0))
        _emit(room, acc, moist, probes)
        # The class a room was graded against, said on the row: a reader
        # comparing two halls has to know they were held to different limits.
        klass, rate = classes.get(room["id"], (None, None))
        room["envelope"]["ashrae_class"] = klass or DEFAULT_CLASS
        room["envelope"]["classified"] = klass is not None
        room["envelope"]["max_rate_k_per_h"] = (
            float(rate) if rate is not None
            else envelope_for(klass).max_rate_k_per_h)
        # Carry the room's verdict upward weighted by the sensors it applied
        # to, so a 44-sensor hall counts for more than a 4-sensor one.
        if moist is not None and acc.get("sensors"):
            for up in (site_acc.setdefault(room["site_id"], _blank()), total_acc):
                up["moist_weighted"] += moist * acc["sensors"]
                up["moist_sensors"] += acc["sensors"]
                up["probes"] += probes
        site = site_acc.setdefault(room["site_id"], _blank())
        _add(site, acc)
        _add(total_acc, acc)

    def _rolled(acc: dict[str, Any]) -> tuple[float | None, int]:
        n = acc.get("moist_sensors", 0.0)
        if not n:
            return None, int(acc.get("probes", 0.0))
        return round(acc["moist_weighted"] / n, 1), int(acc.get("probes", 0.0))

    for site in sites:
        acc = site_acc.get(site["id"], _blank())
        _emit(site, acc, *_rolled(acc))
    _emit(totals, total_acc, *_rolled(total_acc))


def _leg_pct(total: float | None, n: float) -> float | None:
    """A leg's share as a percentage, or None when nothing measured it. Zero
    sensors is not zero compliance.

    Named apart from `_pct` above deliberately: this file already had one, and
    a second definition of that name silently replaced it for every caller in
    the module. They happen to agree, which is exactly why it would not have
    been noticed."""
    if total is None or not n:
        return None
    return round(100.0 * float(total) / float(n), 1)


async def _envelope_by_rack(session: AsyncSession, skeleton: list[dict[str, Any]],
                            *, start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
    """Moisture and rate per rack, each room against its own ASHRAE class.

    Temperature is graded in the main query because every intake source carries
    it. The other two legs cannot be: moisture needs a probe that reports
    humidity at the same instant as temperature, and rate needs a run of
    readings from one sensor. Both are absent on a rack read by server BMCs -
    which is missing evidence, not a pass, and the counts returned here say so.

    The limits are resolved per ROOM and handed to the query, because a hall of
    A3 kit is allowed to 40 C by the same standard that stops A1 at 32; grading
    both against A1 puts a critical row on equipment running inside its own
    specification, and an alarm that is always on is one nobody reads.
    """
    limits = []
    for room in skeleton:
        rate = room.get("max_rate_k_per_h")
        env = envelope_for(room.get("ashrae_class"),
                           float(rate) if rate is not None else None)
        limits.append({
            "room_id": room["room_id"],
            "rec_low": env.rec_low_c, "rec_high": env.rec_high_c,
            "allow_low": env.allow_low_c, "allow_high": env.allow_high_c,
            "rec_dp_low": env.rec_dp_low_c, "rec_dp_high": env.rec_dp_high_c,
            "rec_rh_high": env.rec_rh_high_pct,
            "allow_dp_low": env.allow_dp_low_c, "allow_dp_high": env.allow_dp_high_c,
            "allow_rh_low": env.allow_rh_low_pct, "allow_rh_high": env.allow_rh_high_pct,
            "max_rate": env.max_rate_k_per_h,
        })
    if not limits:
        return {}
    rows = await repo.thermal_envelope(session, start=start, end=end, limits=limits)
    return {str(r["rack_id"]): r for r in rows}


async def thermal(session: AsyncSession, *, focus: date | None = None,
                  compare: date | None = None,
                  mode: str = "daily",
                  source: str = "auto") -> dict[str, Any]:
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

    forced = source if source in SOURCE_METRIC else ""
    skeleton = await repo.thermal_rooms(session)
    if mode == "now":
        raw_racks = await repo.thermal_racks_now(
            session, since=f0, was_at=was_at,
            low_c=BAND_LOW_C, high_c=BAND_HIGH_C,
            allowable_c=ALLOWABLE_HIGH_C)
        p90 = await repo.thermal_p90_now(session, since=f0,
                                         force=SOURCE_METRIC.get(forced, ""))
    else:
        raw_racks = await repo.thermal_racks(session, focus_start=f0, focus_end=f1,
                                             compare_start=c0, compare_end=c1,
                                             low_c=BAND_LOW_C, high_c=BAND_HIGH_C,
                                             allowable_c=ALLOWABLE_HIGH_C)
        # A percentile does not fold from sums, so it is taken over the pooled
        # readings at every tier in one query and attached afterwards. Same
        # readings, same source rule, so it agrees with the averages beside it.
        p90 = await repo.thermal_p90(session, focus_start=f0, focus_end=f1,
                                     force=SOURCE_METRIC.get(forced, ""))

    # The other two legs of the envelope, per rack, graded against each room's
    # own class. Temperature is scored above from every intake source; moisture
    # and rate can only be scored where a probe reports the pair at one instant,
    # so this is a second pass rather than more columns on the first.
    envelope = await _envelope_by_rack(session, skeleton, start=f0, end=f1)

    silent = ("no sensor in this rack has reported in the last ten minutes"
              if mode == "now" else
              "no intake probe or server sensor in this rack reported in this window")
    # Only the NOW view measures over a span short and fixed enough for a
    # rate to mean anything. See _derive.
    rate_hours = (repo.RATE_WINDOW.total_seconds() / 3600.0
                  if mode == "now" else None)
    racks = [_rack_row(r, absent_now, absent_prev, silent, rate_hours, forced)
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
    _attach_envelope(racks, rooms, sites, totals, envelope, skeleton)
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
        "source": forced or "auto",
        "totals": totals,
        "sites": sites,
        "rooms": [_strip(r) for r in rooms],
        "racks": [_strip(r) for r in racks],
        "notes": [_source_note(totals, forced), _humidity_note(totals),
                  _distribution_note(mode), *_window_note(mode)],
    }


# The private keys every tier carries so the tier above can fold it. Sums and
# counts, never averages; maxima; and how many racks each source spoke for.
#
# EVERY SUM HERE IS PER SENSOR, and `_band_n` - how many sensors spoke - is
# what all of them divide by. `_sum` is the sum of the sensors' own means, so
# the quotient is a mean of means; `_in_band`, `_below` and `_hot` are
# sensor-TIME, each sensor's share of the window it spent in that band, so
# they run from 0 to `_band_n`. Both shapes fold by plain addition, which is
# the point: a tier adds its children without a weight travelling beside
# them, and the answer is still the mean across every sensor underneath.
#
# `_n` is the raw reading count and divides nothing. It survives for the
# Readings column and for the note that says a window was silent.
#
# `_grade_n` is `_band_n` again with the UNGRADED rooms taken out, and it is
# what the ASHRAE figures divide by. The two are different populations on
# purpose: a generator room has a temperature, and it does not have a
# compliance. Sharing one divisor cost the room its average the first time
# this was written.
_ACC_SUMS = ("_sum", "_n", "_band_n", "_grade_n", "_in_band", "_below", "_hot",
             "_prev_sum", "_prev_n", "_prev_band_n",
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


#: Which columns in a rack row belong to which source: readings, sensors, and
#: the same reading from the far end of the comparison.
_SOURCE_COLUMNS = {
    "probes":  ("p", "pc"),
    "servers": ("f", "c"),
    "network": ("n", "nc"),
}


def _rack_row(r: dict[str, Any], absent_now: str, absent_prev: str,
              absent: str, rate_hours: float | None = None,
              forced: str = "") -> dict[str, Any]:
    p_n = int(r.get("p_n") or 0)
    f_n = int(r.get("f_n") or 0)
    if forced:
        # Pinned by the reader. A rack with nothing from that source reads as
        # silent rather than falling through to another one, which is the
        # whole point: "these fourteen racks have no probe" is the answer
        # somebody asked for.
        now, was = _SOURCE_COLUMNS[forced]
        n = int(r.get(f"{now}_n") or 0)
        source = forced if n else None
        s_sum, mx, in_band = r.get(f"{now}_sum"), r.get(f"{now}_max"), r.get(f"{now}_in_band")
        below, hot = r.get(f"{now}_below"), r.get(f"{now}_hot")
        sensors = int(r.get(f"{now}_sensors") or 0)
        prev_n = int(r.get(f"{was}_n") or 0)
        prev_sensors = int(r.get(f"{was}_sensors") or 0)
        prev_sum, prev_max = r.get(f"{was}_sum"), r.get(f"{was}_max")
    elif p_n:
        source = "probes"
        n, s_sum, mx, in_band = p_n, r["p_sum"], r["p_max"], r["p_in_band"]
        below, hot = r.get("p_below"), r.get("p_hot")
        sensors = int(r.get("p_sensors") or 0)
        prev_n, prev_sum, prev_max = int(r.get("pc_n") or 0), r.get("pc_sum"), r.get("pc_max")
        prev_sensors = int(r.get("pc_sensors") or 0)
    elif f_n:
        source = "servers"
        n, s_sum, mx, in_band = f_n, r["f_sum"], r["f_max"], r["f_in_band"]
        below, hot = r.get("f_below"), r.get("f_hot")
        sensors = int(r.get("f_sensors") or 0)
        prev_n, prev_sum, prev_max = int(r.get("c_n") or 0), r.get("c_sum"), r.get("c_max")
        prev_sensors = int(r.get("c_sensors") or 0)
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
        prev_sensors = int(r.get("nc_sensors") or 0)
    else:
        source, n, s_sum, mx, in_band, sensors = None, 0, 0.0, None, 0, 0
        below = hot = 0
        # Nothing this window; whichever source spoke last window names the
        # comparison for the note, and the delta is None regardless.
        prev_n = int(r.get("pc_n") or r.get("c_n") or r.get("nc_n") or 0)
        prev_sensors = int(r.get("pc_sensors") or r.get("c_sensors")
                           or r.get("nc_sensors") or 0)
        prev_sum, prev_max = None, None
    e_sensors = int(r.get("e_sensors") or 0)
    rh_n = int(r.get("rh_n") or 0)
    _facility = r.get("room_class") == "facility"

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
        # An empty cabinet, as distinct from a silent one. Rows that carry
        # nothing are hidden by default on the rack tier and counted in the
        # toggle that shows them; a populated rack with no sensor stays.
        "devices": int(r.get("devices") or 0),
        "empty": int(r.get("devices") or 0) == 0,
        # `e_sum` is the sum of each exhaust sensor's own mean, so the
        # divisor is how many spoke - not how many times they did.
        "exhaust_c": (round(float(r["e_sum"]) / e_sensors, 1)
                      if e_sensors else None),
        "_sum": float(s_sum or 0.0), "_n": n,
        # Sensor-time, and its divisor. In the windowed views each term is one
        # sensor's share of its own window; in the NOW view a sensor is either
        # in band or not at this instant, which is the same arithmetic with
        # every share a 0 or a 1.
        # ASHRAE, or nothing. The recommended envelope is written for IT
        # equipment INTAKE AIR, classes A1-A4. It does not govern a generator
        # room, a UPS room, a switchgear room or a chiller hall - those are
        # ventilated to their own criteria, and a generator room at 27.5 C is
        # unremarkable rather than 0 % compliant. Grading them put findings on
        # the page that were not findings.
        #
        # It also made the estate figure unreadable. The rooms table hides
        # facility rooms and the facility table carries no in-band column, so
        # the three rooms dragging the estate to 98.3 % appeared in NO view:
        # every room a reader could open said 100 % and the site said 99.1,
        # with nothing on the page to reconcile them. A number that cannot be
        # traced to the rows beneath it is worse than one that is merely
        # wrong, because there is nowhere to go and check.
        #
        # They keep every reading they have - average, max, spread of their own
        # temperatures, alarms - and the per-device inlet rules still fire on
        # anything in them that runs hot, because those are written against
        # devices and not rooms. What stops is the SCORING.
        "_band_n": sensors,
        "_grade_n": 0 if _facility else sensors,
        "_in_band": 0.0 if _facility else float(in_band or 0.0),
        "_below": 0.0 if _facility else float(below or 0.0),
        "_hot": 0.0 if _facility else float(hot or 0.0),
        "_prev_sum": _f(prev_sum) or 0.0, "_prev_n": prev_n,
        "_prev_band_n": prev_sensors,
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
    n, prev_n = acc["_n"], acc["_prev_n"]
    # Every figure here divides by SENSORS. The sums arrive as sums of each
    # sensor's own mean, so this is a mean of means by construction - the one
    # shape a tier can fold without a weight travelling beside it. `_n` is
    # kept for the Readings column and for the note that says a window was
    # silent, which are the only two questions about readings the page asks.
    band_n, prev_band_n = acc["_band_n"], acc["_prev_band_n"]
    grade_n = acc["_grade_n"]
    avg = round(acc["_sum"] / band_n, 1) if band_n else None
    prev_avg = round(acc["_prev_sum"] / prev_band_n, 1) if prev_band_n else None
    mx = None if acc["_max"] is None else round(acc["_max"], 1)
    prev_mx = None if acc["_prev_max"] is None else round(acc["_prev_max"], 1)
    return {
        "avg_c": avg,
        "max_c": mx,
        "compliance_pct": _pct(acc["_in_band"], grade_n) if grade_n else None,
        "below_pct": _pct(acc["_below"], grade_n) if grade_n else None,
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
        # Humidity divides by its probes for the same reason the intake
        # average divides by its sensors.
        "rh_avg": (round(acc["_rh_sum"] / acc["_rh_probes"], 1)
                   if acc["_rh_probes"] else None),
        "rh_max": None if acc["_rh_max"] is None else round(acc["_rh_max"], 1),
        "rh_probes": acc["_rh_probes"],
        "sources": {"probes": acc["_probes"], "servers": acc["_servers"],
                    "network": acc["_network"]},
        # Said once per row, so a reader never has to guess whether a blank
        # cell means "cool" or "nobody is measuring".
        "note": None if n else absent,
        # And the third thing a blank in-band cell can mean, which the note
        # above cannot say because the room IS reporting: it is not the kind of
        # room the envelope is written for. Without this a facility room shows
        # a bare dash and the reader is back to guessing.
        "grade_note": (
            "not graded: ASHRAE's recommended envelope is for IT equipment "
            "intake air, and this room has no IT load. Its temperatures are "
            "beside this, and the per-device inlet rules still watch anything "
            "in it that runs hot"
            if n and not grade_n else None),
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
    """Where the sensors' TIME fell against the ASHRAE lines, as shares.

    Four bands that partition the window: below the recommended floor
    (overcooled - the finding a floor most often pays for), inside the
    recommended band, above it but inside the allowable envelope, and above
    the allowable ceiling. Each sensor's own window is what gets split, then
    the splits add, so a room's bar is the mean of its sensors and not a pool
    of readings in which the fastest-polled sensor drew the widest band.
    """
    grade_n = acc["_grade_n"]
    if not grade_n:
        return None
    below, in_band, hot = acc["_below"], acc["_in_band"], acc["_hot"]
    above_rec = max(grade_n - below - in_band - hot, 0.0)
    return {
        "below_pct": _pct(below, grade_n),
        "in_band_pct": _pct(in_band, grade_n),
        "above_recommended_pct": _pct(above_rec, grade_n),
        "above_allowable_pct": _pct(hot, grade_n),
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
    # Sensor-time, so fractional from the first rack rather than after the
    # first addition promotes it.
    acc["_in_band"] = acc["_below"] = acc["_hot"] = 0.0
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
    n = acc["_n"]
    band_n, prev_band_n = acc["_band_n"], acc["_prev_band_n"]
    grade_n = acc["_grade_n"]
    white = [r for r in rooms if r["room_class"] == "white_space"]
    return {
        "avg_c": round(acc["_sum"] / band_n, 1) if band_n else None,
        "max_c": None if acc["_max"] is None else round(acc["_max"], 1),
        "compliance_pct": _pct(acc["_in_band"], grade_n) if grade_n else None,
        "below_pct": _pct(acc["_below"], grade_n) if grade_n else None,
        "distribution": _distribution(acc),
        "samples": n,
        # The estate is going somewhere too, and the headline band is where
        # that is read first. Same arithmetic as every tier below it, so the
        # figure at the top is the rows added up rather than a second opinion.
        "rate_k_per_h": _rate(
            round(acc["_sum"] / band_n, 1) if band_n else None,
            round(acc["_prev_sum"] / prev_band_n, 1) if prev_band_n else None,
            rate_hours),
        "rh_avg": (round(acc["_rh_sum"] / acc["_rh_probes"], 1)
                   if acc["_rh_probes"] else None),
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


def _source_note(totals: dict[str, Any], forced: str = "") -> str:
    probes = int(totals["sources"]["probes"])
    servers = int(totals["sources"]["servers"])
    network = int(totals["sources"].get("network") or 0)
    if forced:
        spoke = {"probes": probes, "servers": servers, "network": network}[forced]
        kind = {"probes": "front environment probe",
                "servers": "servers' BMC inlet sensors",
                "network": "front-panel sensor on the network gear"}[forced]
        return (f"Pinned to one source: every rack is read from its {kind}, "
                f"and a rack without one reads as silent rather than falling "
                f"back. {spoke} rack{'s' if spoke != 1 else ''} answered. Use "
                "this to check one source against another - a probe that has "
                "drifted shows up as a hall that disagrees with itself - and "
                "the automatic rule for everything else, because it picks the "
                "sensor closest to the air the rack actually breathes.")

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
            "ten minutes. Every window on this page weights a sensor once; what "
            "is different here is that a sensor has one reading rather than a "
            "window, so the in-band share is the share of sensors in band at "
            "this instant rather than the time they spent there. Use it while "
            "something is happening: an hour's mean needs an hour to show a "
            "change and holds it for an hour after it clears.",
            f"Rate is the same shot taken again as of {mins} minutes ago, "
            f"divided out to K/hour - the unit ASHRAE writes its "
            f"{RATE_LIMIT_K_PER_H:g} K/hour limit on rate of change in, and the "
            "unit an operator estimates time-to-trouble with. Fixed for every "
            "row, so a rack read by a 120 s probe and one read by 60 s BMCs are "
            f"the same measurement. Under {RATE_NOISE_K_PER_H:g} K/hour is "
            "sensor noise rather than air, and reads as flat. A rack with "
            f"nothing reporting {mins} minutes ago has no rate rather than a "
            "large one."]


def _distribution_note(mode: str) -> str:
    # One vote per intake sensor in every mode. Over a window that vote is the
    # share of the window the sensor spent in each band; at an instant it is
    # simply where the sensor is. Said differently per mode because "time in
    # band" is a promise the NOW view cannot keep - it has no time to divide.
    split = ("splits the intake sensors by the ASHRAE lines, one vote each"
             if mode == "now" else
             "splits each intake sensor's TIME by the ASHRAE lines, then "
             "averages the sensors, so a sensor polled twice as often does "
             "not count twice")
    return (f"Spread {split}: below "
            f"{BAND_LOW_C:g} °C is overcooled, the most common finding on a real "
            f"floor and the evidence for raising a setpoint; {BAND_LOW_C:g}-"
            f"{BAND_HIGH_C:g} °C recommended; {BAND_HIGH_C:g}-{ALLOWABLE_HIGH_C:g} °C "
            f"allowable; above {ALLOWABLE_HIGH_C:g} °C at risk. p90 is the 90th "
            "percentile SENSOR, each taken at its own mean over the focus window, "
            "interpolated: how hot the warm end of the row runs without one "
            "sensor's spike deciding, which Max lets happen.")


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
                live: bool = False, hour: bool = False) -> dict[str, Any]:
    """Rack, room and site power, split IT / cooling / other, judged against
    rating, with PUE per row.

    Three windows, because they answer different questions. NOW (`live`) is
    the hot mirror - what the estate is drawing this minute, compared with
    fifteen minutes ago. LAST HOUR (`hour`) is sixty minutes of five-minute
    buckets against the hour before. BY DAY is the date range given, against
    the equally long range before it. In the windowed modes `mode` picks the
    statistic: AVERAGE is the mean over the window, PEAK is the COINCIDENT
    maximum - loads summed per bucket first, then the largest bucket taken,
    at every tier, so the figure is one that actually happened rather than a
    sum of each room's private worst moment.

    Everything below a room is the rack: its IT draw is the sum of the loads
    in it, the same population the room's IT column sums, so racks add up to
    their room. The rating a load is judged against is the rack's own
    (`rack.rated_power_kw`), the room's design IT kW where the inventory
    carries one and otherwise the sum of its racks' ratings, and the site's
    the same way - and every row says which it used.
    """
    now = datetime.now(UTC)
    if live:
        raw = await repo.power_live(session)
        raw_racks = await repo.power_racks_live(session)
        window = {"mode": "live", "label": "now",
                  "compare_label": f"{int(repo.POWER_PREV_AT.total_seconds() // 60)} minutes ago"}
        peak = False
        peaks: dict[str, Any] = {"sites": {}, "total": {}}
    else:
        if hour:
            end, start = now, now - timedelta(hours=1)
        else:
            end = end or now
            start = start or (end - timedelta(days=1))
        span = end - start
        bucket = _bucket_for(start, end)
        kw = {"start": start, "end": end, "compare_start": start - span,
              "compare_end": start, "bucket": bucket}
        raw = await repo.power_window(session, **kw)
        raw_racks = await repo.power_racks_window(session, **kw)
        peak = mode == "peak"
        # A site's peak cannot be folded from its rooms', so it is read from
        # the same buckets one tier up - only when it is wanted.
        peaks = await repo.power_window_peaks(session, **kw) if peak \
            else {"sites": {}, "total": {}}
        window = {"mode": "hour" if hour else mode, "start": start, "end": end,
                  "bucket_seconds": int(bucket.total_seconds()),
                  "source": repo.power_source(bucket),
                  "compare_start": start - span, "compare_end": start,
                  "label": "last hour" if hour else _span_label(start, end),
                  "compare_label": "the hour before" if hour
                  else "the equally long window before it"}

    inv = await repo.power_inventory(session)
    alarms = await repo.thermal_alarms(session, categories=list(POWER_ALARM_CATEGORIES))

    racks = [_power_rack_row(r, inv, peak=peak) for r in raw_racks]
    rooms = [_power_row(r, peak=peak) for r in raw]
    _attach_racks(rooms, racks, inv["room_design_kw"])
    sites = _fold_power_sites(rooms, inv["site_design_kw"], peaks["sites"])
    totals = _fold_power_total(rooms, racks, peaks["total"])

    _attach_count(racks, alarms["racks"])
    _attach_count(rooms, alarms["rooms"])
    _attach_count(sites, alarms["sites"])
    totals["alarms_open"] = alarms["total"]
    # How many of a row's conditions are on something IN a rack. A UPS or an
    # RPP stands on the floor, so a hall can show open power conditions
    # while every rack under it shows none - correct, and unreadable
    # without saying which.
    in_racks: dict[str, int] = {}
    for rack in racks:
        in_racks[rack["room_id"]] = in_racks.get(rack["room_id"], 0) + rack["alarms_open"]
    for room in rooms:
        room["alarms_in_racks"] = in_racks.get(room["id"], 0)
    by_site: dict[str, int] = {}
    for room in rooms:
        by_site[room["site_id"]] = by_site.get(room["site_id"], 0) + room["alarms_in_racks"]
    for site in sites:
        site["alarms_in_racks"] = by_site.get(site["id"], 0)
    totals["alarms_in_racks"] = sum(r["alarms_in_racks"] for r in rooms)

    # Planning figures: headroom against rating and stranded allocation from
    # the seven-day p95, the energy PUE beside the ratio, and whether a
    # rack's two sides share a panel, a UPS or a transfer switch.
    await _attach_headroom(session, rooms, racks, sites, totals)
    pue_end = now if live else end
    pue_start = pue_end - timedelta(hours=24) if live else start
    await _attach_energy_pue(session, sites, totals, start=pue_start, end=pue_end)
    await _attach_shared_upstream(session, racks)
    await _attach_legs_and_supplies(session, racks, rooms, sites, totals)
    await _attach_failover_and_banks(session, racks, rooms, sites, totals)

    # When the newest figure on the page was written. In the windowed modes
    # the window end is the answer; in NOW it is the newest hot-mirror write.
    as_of = max((r.get("as_of") for r in raw if r.get("as_of")), default=None) \
        if live else end
    totals["as_of"] = as_of

    notes = [
        "IT(DC) is blank throughout: nothing meters a DC bus in this estate, "
        "so the column exists for parity with sites that do.",
        "A rack's load is the draw its servers and switches report, the same "
        "population the room's IT column sums, so a room's racks add up to "
        "the room. The strip's own meter, read on the rack page, sits a few "
        "percent above it (PSU losses).",
    ]
    src = window.get("source")
    if src == "telemetry_5m":
        notes.append("This window reads the five-minute rollup, which trails the "
                     "readings by about five minutes.")
    elif src == "telemetry_1h":
        notes.append("This window reads the hourly rollup, which trails the "
                     "readings by up to an hour.")
    if peak:
        notes.append("Peak is coincident at every tier - loads are summed per "
                     "bucket before the maximum is taken, so a site's peak is "
                     "a moment that happened, not its rooms' peaks added up.")
    return {
        "window": window,
        "bands": dict(POWER_BANDS),
        "alarm_categories": list(POWER_ALARM_CATEGORIES),
        "totals": totals,
        "sites": sites,
        "rooms": [_strip(r) for r in rooms],
        "racks": [_strip(r) for r in racks],
        "notes": notes,
    }


def _r1(v: Any) -> float | None:
    f = _f(v)
    return None if f is None else round(f, 1)


def _span_label(start: datetime, end: datetime) -> str:
    """"2026-10-20 to 2026-10-26" for a range whose end is the midnight after
    the last day, which is what the page sends for an inclusive pick."""
    last = (end - timedelta(seconds=1)).date()
    return f"{start.date().isoformat()} to {last.isoformat()}"


def _band(load_pct: float | None) -> str | None:
    """ok / warn / critical against POWER_BANDS, None where there is no ratio."""
    if load_pct is None:
        return None
    if load_pct >= POWER_BANDS["critical_pct"]:
        return "critical"
    if load_pct >= POWER_BANDS["warn_pct"]:
        return "warn"
    return "ok"


def _load_pct(load: float | None, rated: float | None) -> float | None:
    if load is None or not rated:
        return None
    return round(100.0 * load / rated, 1)


_SPREAD_KEYS = ("under_60", "b60_80", "b80_90", "over_90", "unrated")


def _spread_bucket(load_pct: float | None) -> str:
    if load_pct is None:
        return "unrated"
    if load_pct >= 90:
        return "over_90"
    if load_pct >= 80:
        return "b80_90"
    if load_pct >= 60:
        return "b60_80"
    return "under_60"


def _power_rack_row(r: dict[str, Any], inv: dict[str, Any], *, peak: bool) -> dict[str, Any]:
    it = _f(r.get("peak_it" if peak else "avg_it"))
    it = None if it is None else round(it, 2)
    prev = _f(r.get("prev_peak_it" if peak else "prev_it"))
    rated = _f(r.get("rated_power_kw"))
    rated = rated if rated and rated > 0 else None
    plan = inv["racks"].get(r["rack_id"], {})
    feeds = inv["feeds"].get(r["rack_id"])
    loads = int(r.get("loads") or 0)
    reporting = int(r.get("reporting") or 0) if "reporting" in r else None
    pct = _load_pct(it, rated)
    if it is None:
        note = ("nothing in this rack reports power" if loads
                else "no IT or network device in this rack")
    else:
        note = None
    allocated = _f(plan.get("allocated_kw"))
    return {
        "id": r["rack_id"], "kind": "rack", "name": r["rack_name"],
        "room_id": r["room_id"], "room_name": r["room_name"],
        "floor": r["floor"], "room_class": r["room_class"],
        "site_id": r["datacenter_id"], "site_code": r["site_code"],
        "site_name": r["site_name"],
        "total_kw": it, "it_ac_kw": it, "it_dc_kw": None,
        "cooling_kw": None, "other_kw": None, "pue": None,
        "delta_total": _delta(it, None if prev is None else round(prev, 2)),
        "rated_kw": rated,
        "rating_basis": "rack" if rated else None,
        "load_pct": pct,
        "band": _band(pct),
        "allocated_kw": None if allocated is None else round(allocated, 2),
        "reserved_kw": (None if plan.get("reserved_kw") is None
                        else round(float(plan["reserved_kw"]), 2)),
        "unrated_devices": int(plan.get("unrated") or 0),
        "feeds": feeds,
        "loads": loads,
        "devices": int(r.get("devices") or 0),
        "empty": int(r.get("devices") or 0) == 0,
        "reporting": reporting,
        "note": note,
        "_it": it, "_cooling": None, "_other": None, "_total": it,
        "_prev": None if prev is None else round(prev, 2),
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

    prev = _f(r.get("prev_peak_total" if peak else "prev_total"))
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
        "reporting": (None if r.get("reporting") is None
                      else {"devices": int(r.get("devices") or 0),
                            "reporting": int(r.get("reporting") or 0)}),
        "note": None if total is not None else "no power meter reported here",
        "_it": it, "_cooling": cooling, "_other": other, "_total": total,
        "_prev": None if prev is None else round(prev, 1),
    }


def _attach_racks(rooms: list[dict[str, Any]], racks: list[dict[str, Any]],
                  room_design_kw: dict[str, float]) -> None:
    """Rating, load against it and the spread of its racks, per room.

    The room's rating is its design IT kW when the inventory carries one,
    else the sum of its racks' ratings - and the row says which. The load
    judged against it is the IT draw, because both ratings are IT ratings;
    cooling is on another feed and another budget.
    """
    by_room: dict[str, list[dict[str, Any]]] = {}
    for rk in racks:
        by_room.setdefault(rk["room_id"], []).append(rk)
    for room in rooms:
        rks = by_room.get(room["id"], [])
        rack_rated = sum(rk["rated_kw"] for rk in rks if rk["rated_kw"])
        design = room_design_kw.get(room["id"])
        rated = design if design else (round(rack_rated, 1) if rack_rated else None)
        basis = "design" if design else ("racks" if rack_rated else None)
        spread = dict.fromkeys(_SPREAD_KEYS, 0)
        for rk in rks:
            spread[_spread_bucket(rk["load_pct"])] += 1
        pct = _load_pct(room["_it"], rated)
        room.update({
            "rated_kw": rated, "rating_basis": basis,
            "load_pct": pct, "band": _band(pct),
            "rack_count": len(rks),
            "racks_rated": sum(1 for rk in rks if rk["rated_kw"]),
            "racks_reporting": sum(1 for rk in rks if rk["_it"] is not None),
            "spread": spread if rks else None,
            "allocated_kw": (round(sum(rk["allocated_kw"] for rk in rks
                                       if rk["allocated_kw"] is not None), 1)
                             if any(rk["allocated_kw"] is not None for rk in rks) else None),
            "single_cord": sum((rk["feeds"] or {}).get("single_cord", 0) for rk in rks),
            "unfed": sum((rk["feeds"] or {}).get("unfed", 0) for rk in rks),
            "_rack_rated": rack_rated,
        })


def _fold_power_sites(rooms: list[dict[str, Any]], site_design_kw: dict[str, float],
                      peaks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for r in rooms:
        s = by_site.setdefault(r["site_id"], {
            "id": r["site_id"], "kind": "site", "name": r["site_name"],
            "site_id": r["site_id"], "site_code": r["site_code"],
            "site_name": r["site_name"], "room_count": 0, "rack_count": 0,
            "racks_rated": 0, "racks_reporting": 0,
            "_it": None, "_cooling": None, "_other": None, "_total": None,
            "_prev": None, "_rack_rated": 0.0,
            "spread": dict.fromkeys(_SPREAD_KEYS, 0),
            "allocated_kw": None, "single_cord": 0, "unfed": 0,
            "alarms_open": 0,
        })
        s["room_count"] += 1
        s["rack_count"] += r.get("rack_count", 0)
        s["racks_rated"] += r.get("racks_rated", 0)
        s["racks_reporting"] += r.get("racks_reporting", 0)
        # A site without a design figure is rated as the sum of its rooms'
        # ratings, each by its own rule, so a room with a design figure keeps
        # it in the site's total instead of being replaced by its racks'.
        s["_rack_rated"] += r.get("rated_kw") or 0.0
        s["single_cord"] += r.get("single_cord", 0)
        s["unfed"] += r.get("unfed", 0)
        if r.get("spread"):
            for k in _SPREAD_KEYS:
                s["spread"][k] += r["spread"][k]
        if r.get("allocated_kw") is not None:
            s["allocated_kw"] = round((s["allocated_kw"] or 0) + r["allocated_kw"], 1)
        for key in ("_it", "_cooling", "_other", "_total", "_prev"):
            if r[key] is not None:
                s[key] = r[key] if s[key] is None else round(s[key] + r[key], 1)

    out = []
    for s in by_site.values():
        # In PEAK mode the sum of room peaks is a moment that never happened;
        # the coincident figure comes from the bucket query one tier up.
        pk = peaks.get(s["id"])
        if pk:
            for key, col in (("_it", "peak_it"), ("_cooling", "peak_cooling"),
                             ("_other", "peak_other"), ("_total", "peak_total")):
                v = _f(pk.get(col))
                s[key] = None if v is None else round(v, 1)
            pv = _f(pk.get("prev_peak_total"))
            s["_prev"] = None if pv is None else round(pv, 1)
        total, it = s["_total"], s["_it"]
        design = site_design_kw.get(s["id"])
        rated = design if design else (round(s["_rack_rated"], 1) if s["_rack_rated"] else None)
        pct = _load_pct(it, rated)
        out.append(_strip({**s,
                           "total_kw": total, "it_ac_kw": it, "it_dc_kw": None,
                           "cooling_kw": s["_cooling"], "other_kw": s["_other"],
                           "pue": round(total / it, 3) if total and it else None,
                           "delta_total": _delta(total, s["_prev"]),
                           "rated_kw": rated,
                           "rating_basis": "design" if design else ("racks" if rated else None),
                           "load_pct": pct, "band": _band(pct),
                           "spread": s["spread"] if s["rack_count"] else None,
                           "note": None if total is not None else "no power meter reported"}))
    return sorted(out, key=lambda r: r["site_code"])


def _fold_power_total(rooms: list[dict[str, Any]], racks: list[dict[str, Any]],
                      peak: dict[str, Any]) -> dict[str, Any]:
    def total(key: str, subset: list[dict[str, Any]] | None = None) -> float | None:
        vals = [r[key] for r in (subset or rooms) if r[key] is not None]
        return round(sum(vals), 1) if vals else None

    it, all_kw = total("_it"), total("_total")
    cooling, other = total("_cooling"), total("_other")
    if peak:
        it, all_kw, cooling, other = (_r1(peak.get(k)) for k in
                                      ("peak_it", "peak_total", "peak_cooling", "peak_other"))
    facility = [r for r in rooms if r["room_class"] != "white_space"]
    rated = sum(r["rated_kw"] for r in rooms if r.get("rated_kw"))
    rated = round(rated, 1) if rated else None
    spread = dict.fromkeys(_SPREAD_KEYS, 0)
    for rk in racks:
        spread[_spread_bucket(rk["load_pct"])] += 1
    pct = _load_pct(it, rated)
    return {"total_kw": all_kw, "it_ac_kw": it, "it_dc_kw": None,
            "cooling_kw": cooling, "other_kw": other,
            "pue": round(all_kw / it, 3) if all_kw and it else None,
            "rooms_reporting": sum(1 for r in rooms if r["_total"] is not None),
            "rooms": len(rooms),
            "halls": sum(1 for r in rooms if r["room_class"] == "white_space"),
            "halls_reporting": sum(1 for r in rooms
                                   if r["room_class"] == "white_space"
                                   and r["_total"] is not None),
            "racks": len(racks),
            "racks_reporting": sum(1 for rk in racks if rk["_it"] is not None),
            "rated_kw": rated,
            "rating_basis": "rooms" if rated else None,
            "load_pct": pct, "band": _band(pct),
            "spread": spread if racks else None,
            "single_cord": sum(r.get("single_cord", 0) for r in rooms),
            "unfed": sum(r.get("unfed", 0) for r in rooms),
            # What the facility rooms contribute. The UI hides those rows by
            # default, and a header that does not match the visible rows has to
            # say why.
            "facility": {"rooms": len(facility),
                         "total_kw": total("_total", facility) if facility else None,
                         "cooling_kw": total("_cooling", facility) if facility else None}}


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


def _trend_window(*, days: int, bucket: str, since: date | None,
                  until: date | None) -> tuple[datetime, datetime, timedelta, int]:
    """The grid both thermal trends are drawn on: start, end, bucket width
    and how many days that came to.

    The last `days` - hourly ending at the top of the NEXT hour, so the
    bucket in progress is drawn; daily ending at the start of tomorrow, UTC -
    or exactly `since`..`until` inclusive when both are given.

    Shared because the intake line and the compliance columns are two views
    of one window: a grid derived twice would eventually drift by an hour in
    one of them, and the two cells of the chart's view control would stop
    being comparable.
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
    return start, end, width, days


async def thermal_trend(session: AsyncSession, *, days: int = 7,
                        bucket: str = "hour", since: date | None = None,
                        until: date | None = None, room_id: str | None = None,
                        datacenter_id: str | None = None,
                        rack_id: str | None = None,
                        source: str = "auto") -> dict[str, Any]:
    """Intake average, p90 and max per hour or per day, in one scope.

    The table's Δ columns compare two windows and nothing more; this is the
    run-up. Same readings, same probe-first source rule, drawn against the
    ASHRAE band so "warm" has a shape - a hall drifting up over a week and a
    hall that spiked this morning are different problems wearing the same
    Max.

    `source` pins every rack to one intake source, exactly as it does for the
    table. It has to: a reader who pinned the table to PROBES and got a chart
    still drawn the automatic way would be looking at two measurements of one
    estate, on one screen, with nothing saying they differed.

    The window is the last `days` (hourly: ending at the top of the next
    hour, so the bucket in progress is drawn; daily: UTC days ending today),
    or exactly `since`..`until` inclusive when both are given. Every bucket
    in the window is present, oldest first; one nothing reported in carries
    nulls, so the line breaks there instead of bridging a period nobody
    measured.
    """
    start, end, width, days = _trend_window(days=days, bucket=bucket,
                                            since=since, until=until)

    # All three reads below take the same pin. They are one line drawn from
    # three tables - the rollup, its fine tail and the raw newest bucket - and
    # a pin honoured by some of them would put a seam in the middle of it.
    forced = SOURCE_METRIC.get(source, "") if source != "auto" else ""
    src = "5m" if bucket == "hour" and end - start <= _FINE_SOURCE_UP_TO else "1h"
    rows = await repo.thermal_trend(session, start=start, end=end, bucket=bucket,
                                    source=src, force=forced, room_id=room_id,
                                    datacenter_id=datacenter_id, rack_id=rack_id)
    by = {r["b"]: r for r in rows}
    if src == "1h" and bucket == "hour":
        tail = await repo.thermal_trend(session, start=max(start, end - _TAIL_FROM_FINE),
                                        end=end, bucket=bucket, source="5m",
                                        force=forced, room_id=room_id,
                                        datacenter_id=datacenter_id,
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
                                         force=forced, room_id=room_id,
                                         datacenter_id=datacenter_id,
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
        # Which ROLLUP the points came from: what one value per sensor per
        # sub-bucket means for the p90. Deliberately still called `source`,
        # because that is what it has always meant to every reader of this
        # payload - the intake pin is a different question and gets its own
        # name below rather than quietly taking this one over.
        "source": src,
        # Which INTAKE source the racks were read from: "auto", or the one the
        # reader pinned. The table beside the chart reports the same field.
        "intake_source": source,
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


async def thermal_compliance_trend(session: AsyncSession, *, days: int = 7,
                                   bucket: str = "hour", since: date | None = None,
                                   until: date | None = None,
                                   room_id: str | None = None,
                                   datacenter_id: str | None = None,
                                   rack_id: str | None = None,
                                   source: str = "auto") -> dict[str, Any]:
    """Time in band per hour or per day, split four ways, in one scope.

    The intake line says how warm a scope ran; this says how much of it was
    in the envelope, which is the figure a floor is actually judged on and
    the one a colo owes a customer. It is the table's spread bar given a time
    axis: below the recommended floor, inside it, above it but allowable,
    above the allowable ceiling.

    It answers two questions the line cannot. A setpoint raised last Tuesday
    shows as the below-band share collapsing on Tuesday and STAYING down -
    an average moves half a degree and says nothing about whether the change
    held. And overcooling, the finding most floors pay for, is invisible in
    an average that a cold aisle and a warm one average back into band.

    Same window, same rollups, same probe-first source rule and the same
    intake pin as `thermal_trend`, because the two are cells of one control:
    a reader who flips between them is entitled to assume they describe the
    same sensors over the same hours.
    """
    start, end, width, days = _trend_window(days=days, bucket=bucket,
                                            since=since, until=until)
    forced = SOURCE_METRIC.get(source, "") if source != "auto" else ""
    src = "5m" if bucket == "hour" and end - start <= _FINE_SOURCE_UP_TO else "1h"
    bands = {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
             "allowable_c": ALLOWABLE_HIGH_C}

    async def read(roll: str, t0: datetime, t1: datetime) -> list[dict[str, Any]]:
        return await repo.thermal_compliance_trend(
            session, start=t0, end=t1, bucket=bucket, source=roll,
            force=forced, room_id=room_id, datacenter_id=datacenter_id,
            rack_id=rack_id, **bands)

    rows = await read(src, start, end)
    by = {r["b"]: r for r in rows}
    if src == "1h" and bucket == "hour":
        for r in await read("5m", max(start, end - _TAIL_FROM_FINE), end):
            by.setdefault(r["b"], r)
    if bucket == "hour":
        # The newest buckets are replaced, not filled: a partial hour drawn as
        # a finished one is what made a fault look like it had not arrived.
        for r in await read("raw", max(start, end - _RAW_TAIL), end):
            by[r["b"]] = r

    def pct(v: Any) -> float | None:
        return None if v is None else round(float(v) * 100, 1)

    points = []
    t = start
    while t < end:
        r = by.get(t)
        points.append({
            "t": t,
            "below_pct": None if r is None else pct(r["below"]),
            "in_band_pct": None if r is None else pct(r["in_band"]),
            "above_recommended_pct": None if r is None else pct(r["warm"]),
            "above_allowable_pct": None if r is None else pct(r["hot"]),
            "sensors": int(r["sensors"]) if r is not None else 0,
        })
        t += width
    return {
        "days": days,
        "bucket": bucket,
        "source": src,
        "intake_source": source,
        "since": start,
        "until": end,
        "room_id": room_id,
        "datacenter_id": datacenter_id,
        "rack_id": rack_id,
        "band": {"low_c": BAND_LOW_C, "high_c": BAND_HIGH_C,
                 "allowable_high_c": ALLOWABLE_HIGH_C},
        "points": points,
        "buckets_with_data": sum(1 for p in points if p["in_band_pct"] is not None),
        "sensors": max((p["sensors"] for p in points), default=0),
        # The window as one figure, so the caption can say what the columns
        # come to without the reader adding up a hundred of them. Every
        # bucket that reported counts once, whatever it heard from: a quiet
        # hour is an hour of this floor's life, not a smaller one.
        "in_band_pct": _mean([p["in_band_pct"] for p in points]),
        "below_pct": _mean([p["below_pct"] for p in points]),
    }


def _mean(values: list[float | None]) -> float | None:
    seen = [v for v in values if v is not None]
    return round(sum(seen) / len(seen), 1) if seen else None


async def thermal_delta_trend(session: AsyncSession, *, days: int = 7,
                             bucket: str = "hour", since: date | None = None,
                             until: date | None = None,
                             room_id: str | None = None,
                             rack_id: str | None = None,
                             source: str = "auto") -> dict[str, Any]:
    """Intake, exhaust and ΔT per hour or per day, in one room or one rack.

    The airflow question. Intake says how warm a rack is breathing; ΔT says
    whether the air it breathes is doing any work. A rack at 23 C intake
    discharging 31 C is carrying its heat away; the same rack discharging
    26 C is being flushed with air that bypassed the servers, and the plant
    is spending fan power to move it past them. Rising ΔT at flat intake is
    containment improving, falling ΔT at flat intake is containment leaking,
    and an intake reading alone cannot tell those apart - it is the same
    23 C in both.

    ROOM OR RACK ONLY, and this refuses anything wider rather than drawing
    it. ΔT is a difference taken along one supply-to-return path: a hall has
    one, a rack has one, an estate has none. Two halls with opposite
    problems average to a healthy number, which is worse than no line.

    Exhaust comes from servers only - nothing else in a rack reports its own
    discharge - so a room's ΔT speaks for the racks that hold servers. The
    payload says how many that is, beside how many intake sensors the room
    has, so a reader can see when the two legs describe different parts of
    the floor.
    """
    if room_id is None and rack_id is None:
        raise ValueError("ΔT is drawn per room or per rack, not across halls")
    start, end, width, days = _trend_window(days=days, bucket=bucket,
                                            since=since, until=until)
    forced = SOURCE_METRIC.get(source, "") if source != "auto" else ""
    src = "5m" if bucket == "hour" and end - start <= _FINE_SOURCE_UP_TO else "1h"

    async def read(roll: str, t0: datetime, t1: datetime) -> list[dict[str, Any]]:
        return await repo.thermal_delta_trend(
            session, start=t0, end=t1, bucket=bucket, source=roll,
            force=forced, room_id=room_id, rack_id=rack_id)

    rows = await read(src, start, end)
    by = {r["b"]: r for r in rows}
    if src == "1h" and bucket == "hour":
        for r in await read("5m", max(start, end - _TAIL_FROM_FINE), end):
            by.setdefault(r["b"], r)
    if bucket == "hour":
        for r in await read("raw", max(start, end - _RAW_TAIL), end):
            by[r["b"]] = r

    def deg(v: Any) -> float | None:
        return None if v is None else round(float(v), 1)

    points = []
    t = start
    while t < end:
        r = by.get(t)
        intake = None if r is None else deg(r["intake_c"])
        exhaust = None if r is None else deg(r["exhaust_c"])
        points.append({
            "t": t,
            "intake_c": intake,
            "exhaust_c": exhaust,
            # A difference, not a temperature: it exists only where both legs
            # reported. An hour with intake and no exhaust is drawn as a gap
            # in the ΔT line rather than as a rack that stopped heating.
            "delta_k": (None if intake is None or exhaust is None
                        else round(exhaust - intake, 1)),
            "intake_sensors": int(r["intake_sensors"]) if r is not None else 0,
            "exhaust_sensors": int(r["exhaust_sensors"]) if r is not None else 0,
            "racks": int(r["racks"]) if r is not None else 0,
        })
        t += width
    deltas = [p["delta_k"] for p in points if p["delta_k"] is not None]
    return {
        "days": days,
        "bucket": bucket,
        "source": src,
        "intake_source": source,
        "since": start,
        "until": end,
        "room_id": room_id,
        "rack_id": rack_id,
        "points": points,
        "buckets_with_data": len(deltas),
        "intake_sensors": max((p["intake_sensors"] for p in points), default=0),
        "exhaust_sensors": max((p["exhaust_sensors"] for p in points), default=0),
        # How many racks the exhaust leg speaks for. Lower than the room's
        # rack count wherever a rack holds no servers, which is the honest
        # answer and not a gap to be filled.
        "racks": max((p["racks"] for p in points), default=0),
        "delta_k": round(sum(deltas) / len(deltas), 1) if deltas else None,
    }


# ------------------------------------------------------------------- room view


async def room_kpi(session: AsyncSession, room_id: str) -> dict[str, Any] | None:
    """The room drawer: what is in it, how warm, how loaded, how full."""
    ident = await repo.room(session, room_id)
    if ident is None:
        return None

    census = await repo.room_census(session, room_id)
    updated = await repo.room_updated(session, room_id)

    # A facility room gets the plant view instead of the white-space one. See
    # plant_service.room for why the white-space tiles are wrong here rather
    # than merely empty. None for a data hall, which keeps the layout below.
    facility = (None if ident.get("room_class") == "white_space"
                else await plant_service.room(session, room_id))

    th = await thermal(session, mode="live")
    room_thermal = next((r for r in th["rooms"] if r["id"] == room_id), None)

    pw = await power(session, live=True)
    room_power = next((r for r in pw["rooms"] if r["id"] == room_id), None)

    ut = await utilisation(session)
    room_util = next((r for r in ut["rooms"] if r["id"] == room_id), None)

    return {
        "room": ident,
        "is_white_space": ident.get("room_class") == "white_space",
        # Present only for a facility room. The drawer renders this INSTEAD of
        # environmental/power/utilisation, which describe a data hall.
        "facility": facility,
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
            # The full ASHRAE envelope for this room - the same object and the
            # same measure the thermal page's rows carry. The drawer used to
            # show dry bulb alone under "Readings in band", which was honest
            # about itself but meant the two pages answered "is this hall
            # compliant" with different numbers.
            "envelope": (room_thermal or {}).get("envelope"),
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
            # The remainder of the room's metered total. Carried so the
            # composition bar can account for the whole of it - a stacked bar
            # whose segments do not sum to its own total is worse than four
            # separate numbers.
            "other_kw": (room_power or {}).get("other_kw"),
            # IN-ROOM, and it has to be labelled that way wherever it is shown.
            # This is the room's own metered total over its own IT, so the only
            # cooling in it is the air handlers standing in the room. The
            # chillers, towers and pumps that actually reject the heat are in
            # the plant rooms and are not in this figure, which is why a hall
            # reads 1.09 while its site reads 1.31 on the panel one click away.
            # Neither is wrong; they are different boundaries, and a reader who
            # compares them without being told that is being misled by us.
            "pue": (room_power or {}).get("pue"),
            "pue_scope": "in-room",
            "pue_note": ("air handlers in this room only - the chiller plant "
                         "that rejects this heat is not in this boundary, so "
                         "it reads lower than the site PUE and the two are not "
                         "comparable"),
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


def _scope_rating(ratings: dict[str, Any], *, rack_id: str | None,
                  room_id: str | None, datacenter_id: str | None,
                  ) -> tuple[float | None, str | None]:
    """The rating a trend's load is drawn against, by the table's rule.

    A rack: its own. A room or a site: the design IT figure where the
    inventory has one, else the sum of the rack ratings under it. The
    estate: the sum of every room's rating by that same rule, which is what
    the table's header divides by.
    """
    if rack_id is not None:
        r = ratings["racks"].get(rack_id, {})
        v = r.get("design_kw")
        return (v, "rack") if v else (None, None)
    if room_id is not None:
        r = ratings["rooms"].get(room_id, {})
        if r.get("design_kw"):
            return r["design_kw"], "design"
        return (r.get("rack_sum_kw"), "racks") if r.get("rack_sum_kw") else (None, None)
    if datacenter_id is not None:
        r = ratings["sites"].get(datacenter_id, {})
        if r.get("design_kw"):
            return r["design_kw"], "design"
        return (r.get("rack_sum_kw"), "racks") if r.get("rack_sum_kw") else (None, None)
    total = 0.0
    for r in ratings["rooms"].values():
        total += r.get("design_kw") or r.get("rack_sum_kw") or 0.0
    return (round(total, 1), "rooms") if total else (None, None)


async def power_trend(session: AsyncSession, *, days: int = 7,
                      bucket: str = "hour", since: date | None = None,
                      until: date | None = None, room_id: str | None = None,
                      datacenter_id: str | None = None,
                      rack_id: str | None = None) -> dict[str, Any]:
    """IT, cooling, other and total draw per hour or per day, with PUE and
    the load against rating, in one scope.

    The table's Δ column compares two windows and nothing more; this is the
    run-up. Same readings, same category rule (rack PDUs excluded), summed
    per bucket so every total is coincident - which is what makes the
    window's `peak` a moment that happened rather than a sum of private
    maxima.

    Same grid as the thermal trends (`_trend_window`): hourly points for up
    to a week, daily beyond, or exactly `since`..`until`. Hourly points read
    the five-minute rollup for two days and the hourly one beyond, with the
    newest two hours redrawn from the hypertable, because a rollup is never
    more current than its refresh and the bucket in progress is the one
    somebody is watching. Every bucket in the window is present, oldest
    first; one nothing reported in carries nulls so the line breaks there.
    """
    start, end, width, days = _trend_window(days=days, bucket=bucket,
                                            since=since, until=until)
    src = "5m" if bucket == "hour" and end - start <= _FINE_SOURCE_UP_TO else "1h"
    kw = {"bucket": bucket, "room_id": room_id, "datacenter_id": datacenter_id,
          "rack_id": rack_id}
    rows = await repo.power_trend(session, start=start, end=end, source=src, **kw)
    by = {r["b"]: r for r in rows}
    if src == "1h" and bucket == "hour":
        tail = await repo.power_trend(session, start=max(start, end - _TAIL_FROM_FINE),
                                      end=end, source="5m", **kw)
        for r in tail:
            by.setdefault(r["b"], r)
    if bucket == "hour":
        fresh = await repo.power_trend(session, start=max(start, end - _RAW_TAIL),
                                       end=end, source="raw", **kw)
        for r in fresh:
            by[r["b"]] = r

    ratings = await repo.power_ratings(session)
    rated, basis = _scope_rating(ratings, rack_id=rack_id, room_id=room_id,
                                 datacenter_id=datacenter_id)

    points: list[dict[str, Any]] = []
    t = start
    while t < end:
        r = by.get(t)
        if r is None or not r.get("devices"):
            points.append({"t": t, "it_kw": None, "cooling_kw": None, "other_kw": None,
                           "total_kw": None, "pue": None, "load_pct": None,
                           "devices": 0})
        else:
            it = round(float(r["it_kw"]), 2)
            cooling = round(float(r["cooling_kw"]), 2)
            other = round(float(r["other_kw"]), 2)
            total = round(it + cooling + other, 2)
            points.append({
                "t": t, "it_kw": it, "cooling_kw": cooling, "other_kw": other,
                "total_kw": total,
                "pue": round(total / it, 3) if it > 0 and total > 0 else None,
                "load_pct": _load_pct(it, rated),
                "devices": int(r["devices"]),
            })
        t += width

    with_data = [p for p in points if p["total_kw"] is not None]
    peak = max(with_data, key=lambda p: p["total_kw"]) if with_data else None
    return {
        "days": days,
        "bucket": bucket,
        "source": src,
        "since": start,
        "until": end,
        "room_id": room_id,
        "datacenter_id": datacenter_id,
        "rack_id": rack_id,
        "rated_kw": rated,
        "rating_basis": basis,
        "bands": dict(POWER_BANDS),
        "points": points,
        # The window's coincident maximum: the bucket whose summed draw was
        # highest, named so the chart can mark it and the caption can say
        # when. It is the same number the table's PEAK column shows for the
        # same window, by construction.
        "peak": None if peak is None else {
            "t": peak["t"], "total_kw": peak["total_kw"], "it_kw": peak["it_kw"],
            "load_pct": peak["load_pct"]},
        "buckets_with_data": len(with_data),
        "devices": max((p["devices"] for p in points), default=0),
    }


#: What two strips may share above them without it being a finding. A site
#: with one utility feed shares the utility and the switchgear by design, and
#: flagging every rack for that would teach the reader to ignore the flag.
#: An RPP, a UPS or a transfer switch in common is the real thing: the two
#: cords survive nothing above that point.
_SHARED_CONCERN = frozenset({"rpp", "ups", "ats"})


def rack_shared_upstream(strips: list[dict[str, Any]], edges: list[dict[str, Any]],
                         types: dict[str, str]) -> dict[str, list[str]]:
    """Per rack, the RPP / UPS / ATS devices both its sides hang off.

    `strips` is one row per (rack, feeder strip, side); `edges` is every
    power feeder -> load pair; `types` maps device id to type. Each side's
    strips are walked upstream to their closure, and the devices common to
    the A closure and the B closure that are of a concerning type are named.
    A rack with one side only has nothing to share and gets no entry.
    """
    feeders: dict[str, list[str]] = {}
    for e in edges:
        feeders.setdefault(e["load_dev"], []).append(e["feeder"])
    names: dict[str, str] = {}

    def closure(start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(feeders.get(node, []))
        return seen

    by_rack: dict[str, dict[str, set[str]]] = {}
    for s in strips:
        side = (s.get("side") or "").upper()
        if side not in ("A", "B"):
            continue
        by_rack.setdefault(s["rack_id"], {"A": set(), "B": set()})[side].add(s["feeder_id"])
        names[s["feeder_id"]] = s["feeder"]
    out: dict[str, list[str]] = {}
    for rack_id, sides in by_rack.items():
        if not sides["A"] or not sides["B"]:
            continue
        a = set().union(*(closure(f) for f in sides["A"]))
        b = set().union(*(closure(f) for f in sides["B"]))
        shared = [d for d in a & b if types.get(d) in _SHARED_CONCERN]
        if shared:
            out[rack_id] = sorted(shared)
    return out


def _stranded(allocated: float | None, p95: float | None) -> float | None:
    if allocated is None or p95 is None:
        return None
    return round(max(allocated - p95, 0.0), 2)


async def _attach_headroom(session: AsyncSession, rooms: list[dict[str, Any]],
                           racks: list[dict[str, Any]], sites: list[dict[str, Any]],
                           totals: dict[str, Any]) -> None:
    """Seven-day p95 against rating, and allocated against p95, at every tier.

    Headroom is rating minus the p95 of the coincident hourly IT draw: the
    margin a planner has, not the margin this minute has. Stranded is what
    was budgeted for a rack and is not being drawn - allocated minus p95 -
    summed per room and site, with the racks that hold most of it named, so
    "where can the next rack go" has an answer that is not "nowhere".
    """
    p = await repo.power_p95(session)
    totals["p95_days"] = p["days"]
    for rk in racks:
        p95 = p["racks"].get(rk["id"])
        rk["p95_kw"] = p95
        rk["headroom_kw"] = (None if p95 is None or not rk.get("rated_kw")
                             else round(rk["rated_kw"] - p95, 2))
        rk["stranded_kw"] = _stranded(rk.get("allocated_kw"), p95)
    by_room: dict[str, list[dict[str, Any]]] = {}
    for rk in racks:
        by_room.setdefault(rk["room_id"], []).append(rk)
    for room in rooms:
        p95 = p["rooms"].get(room["id"])
        room["p95_kw"] = p95
        room["headroom_kw"] = (None if p95 is None or not room.get("rated_kw")
                               else round(room["rated_kw"] - p95, 1))
        rks = [rk for rk in by_room.get(room["id"], []) if rk["stranded_kw"] is not None]
        room["stranded_kw"] = round(sum(rk["stranded_kw"] for rk in rks), 1) if rks else None
        room["stranded_top"] = [{"name": rk["name"], "kw": rk["stranded_kw"]}
                                for rk in sorted(rks, key=lambda r: -r["stranded_kw"])[:5]]
    for site in sites:
        p95 = p["sites"].get(site["id"])
        site["p95_kw"] = p95
        site["headroom_kw"] = (None if p95 is None or not site.get("rated_kw")
                               else round(site["rated_kw"] - p95, 1))
        vals = [r["stranded_kw"] for r in rooms
                if r["site_id"] == site["id"] and r.get("stranded_kw") is not None]
        site["stranded_kw"] = round(sum(vals), 1) if vals else None
    totals["p95_kw"] = p["total"]
    totals["headroom_kw"] = (None if p["total"] is None or not totals.get("rated_kw")
                             else round(totals["rated_kw"] - p["total"], 1))
    vals = [r["stranded_kw"] for r in rooms if r.get("stranded_kw") is not None]
    totals["stranded_kw"] = round(sum(vals), 1) if vals else None


async def _attach_energy_pue(session: AsyncSession, sites: list[dict[str, Any]],
                             totals: dict[str, Any], *, start: datetime,
                             end: datetime) -> None:
    """The Green Grid figure beside the ratio: kWh over kWh, with its
    category and whether it is plausible, for the estate and each site."""
    from app.services import pue as pue_service

    def shape(r: dict[str, Any]) -> dict[str, Any]:
        return {"pue": r.get("pue"), "method": r.get("method"),
                "category": r.get("category"), "plausible": r.get("plausible"),
                "note": r.get("note"), "it_kwh": r.get("it_kwh"),
                "total_facility_kwh": r.get("total_facility_kwh"),
                "hours": round((end - start).total_seconds() / 3600.0, 1)}
    totals["energy_pue"] = shape(await pue_service.compute(session, start=start, end=end))
    for site in sites:
        site["energy_pue"] = shape(await pue_service.compute(
            session, start=start, end=end, datacenter_id=site["id"]))


async def _attach_legs_and_supplies(session: AsyncSession, racks: list[dict[str, Any]],
                                    rooms: list[dict[str, Any]],
                                    sites: list[dict[str, Any]],
                                    totals: dict[str, Any]) -> None:
    """Per rack: each strip's leg currents with the NEMA imbalance, and the
    loads running on one supply. Both fold upward as counts, because a hall
    with one rack 25 % out of balance and three servers on one PSU is a hall
    with four things to visit, and the room row should say so.
    """
    legs = await repo.power_rack_legs(session)
    psu = await repo.power_rack_psu_failed(session)
    for rk in racks:
        rk["legs"] = legs.get(rk["id"], [])
        worst = [s["imbalance_pct"] for s in rk["legs"] if s.get("imbalance_pct") is not None]
        rk["imbalance_pct"] = max(worst) if worst else None
        rk["psu_failed"] = psu.get(rk["id"], [])
    by_room: dict[str, list[dict[str, Any]]] = {}
    for rk in racks:
        by_room.setdefault(rk["room_id"], []).append(rk)
    for room in rooms:
        rks = by_room.get(room["id"], [])
        room["racks_imbalanced"] = sum(1 for rk in rks
                                       if (rk.get("imbalance_pct") or 0) >= LEG_IMBALANCE_WARN_PCT)
        room["psu_failed"] = sum(len(rk["psu_failed"]) for rk in rks)
    for site in sites:
        mine = [r for r in rooms if r["site_id"] == site["id"]]
        site["racks_imbalanced"] = sum(r.get("racks_imbalanced", 0) for r in mine)
        site["psu_failed"] = sum(r.get("psu_failed", 0) for r in mine)
    totals["racks_imbalanced"] = sum(r.get("racks_imbalanced", 0) for r in rooms)
    totals["psu_failed"] = sum(r.get("psu_failed", 0) for r in rooms)
    totals["leg_imbalance_warn_pct"] = LEG_IMBALANCE_WARN_PCT


#: A bank, a leg or a strip past this share of its rating is the one the next
#: server trips. 80 % is the continuous-load line both the NEC (210.20) and the
#: strip makers' own near-overload default draw.
BANK_WARN_PCT = 80.0


def rack_bank_load(strips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every bank with a breaker, as a share of that breaker, busiest first.
    A bank the strip reports no breaker for is not judged: a 16 A strip has
    none, and its inlet breaker upstream is the protection."""
    out = []
    for s in strips:
        for bank, amps in (s.get("bank_a") or {}).items():
            brk = (s.get("breaker_a") or {}).get(bank)
            if not brk:
                continue
            out.append({"strip": s["strip"], "bank": bank, "amps": round(amps, 1),
                        "breaker_a": brk, "pct": round(100.0 * amps / brk, 1)})
    return sorted(out, key=lambda b: -b["pct"])


#: A receptacle past this share of its own rating is the one to look at: a
#: C13 is a 10 A device, and IEC 60320 gives it no margin above that.
OUTLET_WARN_PCT = 80.0
#: How many receptacles the rack row carries, busiest first. The rest is a
#: count; the page is a drill, not an inventory.
OUTLET_ROWS = 8


def rack_outlet_metering(strips: list[dict[str, Any]],
                         loads: dict[str, dict[int, str]] | None = None
                         ) -> dict[str, Any] | None:
    """Per-receptacle current on this rack's strips, where a strip reports it.

    Returns None when NO strip on the rack publishes an outlet current -
    which is the ordinary case: a Metered AP8886 meters its inlet, legs and
    banks and has no CT behind its receptacles, and a Switched AP8953 has a
    relay per outlet and no CT. Only a metered-by-outlet unit (Raritan
    PX3-4xxx/5xxx, APC AP84xx/AP86xx) can say what one receptacle draws, so
    an outlet with no sample is unmetered, not idle. Where any strip does
    report, the row says how many of the rack's strips do and lists the
    busiest receptacles with what is corded to each, judged against the
    receptacle's own rating (C13 = 10 A, C19 = 16 A; the connector is not
    on the sample, so the C13 figure is used - the conservative one).
    """
    loads = loads or {}
    metered = [s for s in strips if s.get("outlet_a")]
    if not metered:
        return None
    rows: list[dict[str, Any]] = []
    idle = 0
    for s in metered:
        corded = loads.get(s.get("strip_id") or "", {})
        for outlet, amps in s["outlet_a"].items():
            load = corded.get(outlet)
            if amps <= 0.05 and not load:
                idle += 1
                continue
            rows.append({"strip": s["strip"], "outlet": outlet, "amps": round(amps, 2),
                         "load": load, "pct": round(100.0 * amps / 10.0, 1)})
    rows.sort(key=lambda r: -r["amps"])
    return {
        "strips_metered": len(metered), "strips": len(strips),
        "max_a": rows[0]["amps"] if rows else 0.0,
        "hot": sum(1 for r in rows if r["pct"] >= OUTLET_WARN_PCT),
        "idle": idle,
        "rows": rows[:OUTLET_ROWS], "more": max(0, len(rows) - OUTLET_ROWS),
    }


def rack_failover(strips: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the surviving side carries when one side of a 2N rack is lost.

    Each side is judged as the survivor of the other's loss. Three levels, and
    the worst one is the answer, because each trips on its own:

      strip  everything the rack draws on both sides, on the survivor's
             summed rating. Assumes every load is dual-corded: a load on
             the lost side alone goes dark rather than moving, so this
             overstates a rack with single-corded loads - which the Feeds
             column already flags.
      leg    the two strips' same-numbered legs added, on the survivor's
             per-leg rating (its rating over phases x V L-N).
      bank   the two strips' same-numbered banks added, on the survivor's
             bank breaker.

    Leg and bank assume mirrored cording - a load's two cords in the same
    outlet position on each strip - which is how racks are cabled and how
    this estate is, but is not something the platform can see. They are only
    computed for one strip per side; two strips a side are judged at strip
    level only. None where a rack is not fed from both sides, or a side has
    no draw.
    """
    sides = {k: [s for s in strips if s.get("side") == k] for k in ("A", "B")}
    if not sides["A"] or not sides["B"]:
        return None
    fed = sides["A"] + sides["B"]
    if any(s.get("kw") is None for s in fed):
        return None
    total_kw = sum(s["kw"] for s in fed)
    cases: list[dict[str, Any]] = []
    for lost, keep in (("A", "B"), ("B", "A")):
        surv = sides[keep]
        names = ", ".join(s["strip"] for s in surv)
        if all(s.get("rated_kw") for s in surv):
            rated = sum(s["rated_kw"] for s in surv)
            cases.append({
                "pct": round(100.0 * total_kw / rated, 1), "basis": "strip",
                "lost_side": lost, "survivor": names,
                "detail": f"{total_kw:.1f} kW on {rated:.1f} kW rated"})
        if len(sides["A"]) != 1 or len(sides["B"]) != 1:
            continue
        a, b = sides["A"][0], sides["B"][0]
        s_ = b if lost == "A" else a
        legs_a, legs_b = a.get("legs") or [], b.get("legs") or []
        if (legs_a and len(legs_a) == len(legs_b) and s_.get("rated_kw")
                and s_.get("v_ln")):
            leg_rating = s_["rated_kw"] * 1000.0 / (len(legs_a) * s_["v_ln"])
            for i, (x, y) in enumerate(zip(legs_a, legs_b, strict=True), 1):
                cases.append({
                    "pct": round(100.0 * (x + y) / leg_rating, 1), "basis": "leg",
                    "lost_side": lost, "survivor": s_["strip"],
                    "detail": f"leg {i}: {x + y:.1f} A on {leg_rating:.0f} A"})
        banks_a, banks_b = a.get("bank_a") or {}, b.get("bank_a") or {}
        for bank in sorted(set(banks_a) & set(banks_b)):
            brk = (s_.get("breaker_a") or {}).get(bank)
            if not brk:
                continue
            amps = banks_a[bank] + banks_b[bank]
            cases.append({
                "pct": round(100.0 * amps / brk, 1), "basis": "bank",
                "lost_side": lost, "survivor": s_["strip"],
                "detail": f"bank {bank}: {amps:.1f} A on a {brk:.0f} A breaker"})
    if not cases:
        return None
    worst = max(cases, key=lambda c: c["pct"])
    return {**worst, "total_kw": round(total_kw, 2),
            "mirrored": worst["basis"] in ("leg", "bank"),
            "band": _band(worst["pct"])}


async def _attach_failover_and_banks(session: AsyncSession, racks: list[dict[str, Any]],
                                     rooms: list[dict[str, Any]],
                                     sites: list[dict[str, Any]],
                                     totals: dict[str, Any]) -> None:
    """Per rack: the worst bank against its breaker now, and what the survivor
    carries if a side is lost. Counted up the tiers as racks to visit."""
    state = await repo.power_rack_strip_state(session)
    # The cabling record is read only where a strip meters its receptacles;
    # most do not, and the join is not worth running for a page of dashes.
    outlet_loads: dict[str, dict[int, str]] = {}
    if any(s.get("outlet_a") for strips in state.values() for s in strips):
        outlet_loads = await repo.power_rack_outlet_loads(session)
    warn = POWER_BANDS["warn_pct"]
    for rk in racks:
        strips = state.get(rk["id"], [])
        banks = rack_bank_load(strips)
        rk["banks"] = banks[:6]
        rk["bank_worst_pct"] = banks[0]["pct"] if banks else None
        rk["failover"] = rack_failover(strips)
        rk["outlets"] = rack_outlet_metering(strips, outlet_loads)
    by_room: dict[str, list[dict[str, Any]]] = {}
    for rk in racks:
        by_room.setdefault(rk["room_id"], []).append(rk)
    for room in rooms:
        rks = by_room.get(room["id"], [])
        room["racks_failover_over"] = sum(
            1 for rk in rks if rk.get("failover") and rk["failover"]["pct"] >= warn)
        room["racks_bank_hot"] = sum(
            1 for rk in rks if (rk.get("bank_worst_pct") or 0) >= BANK_WARN_PCT)
    for site in sites:
        mine = [r for r in rooms if r["site_id"] == site["id"]]
        site["racks_failover_over"] = sum(r.get("racks_failover_over", 0) for r in mine)
        site["racks_bank_hot"] = sum(r.get("racks_bank_hot", 0) for r in mine)
    totals["racks_failover_over"] = sum(r.get("racks_failover_over", 0) for r in rooms)
    totals["racks_bank_hot"] = sum(r.get("racks_bank_hot", 0) for r in rooms)
    totals["bank_warn_pct"] = BANK_WARN_PCT
    totals["outlet_warn_pct"] = OUTLET_WARN_PCT
    totals["racks_outlet_metered"] = sum(1 for rk in racks if rk.get("outlets"))


#: NEMA MG-1 derates a motor above 1 % voltage imbalance; on a rack strip the
#: figure is CURRENT imbalance, which is a loading question rather than a
#: motor one: a leg carrying 10 % more than its siblings reaches its breaker
#: 10 % sooner, and past 20 % one leg is doing the work of two.
LEG_IMBALANCE_WARN_PCT = 10.0


async def _attach_shared_upstream(session: AsyncSession, racks: list[dict[str, Any]]) -> None:
    from app.repositories import power as power_repo
    strips = await repo.power_strip_edges(session)
    edges = await power_repo.power_edges(session)
    hops = await power_repo.hop_states(session)
    types = {k: v["device_type"] for k, v in hops.items()}
    names = {k: v["name"] for k, v in hops.items()}
    shared = rack_shared_upstream(strips, edges, types)
    for rk in racks:
        if rk.get("feeds") is not None:
            rk["feeds"] = {**rk["feeds"],
                           "shared_upstream": [names.get(d, d) for d in shared.get(rk["id"], [])]}


async def room_panels(session: AsyncSession, room_id: str) -> dict[str, Any]:
    """Every power panel in a room, each read through the meter clamped on it."""
    from app.services import panel_metering
    panels = []
    for p in await repo.room_panels(session, room_id):
        m = await panel_metering.device_metering(session, p["device_id"])
        panels.append({"device_id": p["device_id"], "name": p["name"],
                       "meters": [mm.model_dump() if hasattr(mm, "model_dump") else mm
                                  for mm in m.meters]})
    return {"room_id": room_id, "panels": panels}
