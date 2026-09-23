"""Home-page site overview and the per-site KPI panel.

Two calls, not twelve. `overview()` backs the table and the alert strip;
`kpi()` backs the drawer that opens when an operator clicks KPIs on a row.

Where a number cannot be computed from what is instrumented, this returns null
WITH A REASON rather than a plausible-looking figure. A DCIM that guesses at
WUE is worse than one that admits it has no water meter, because the guess ends
up in a sustainability report.

The corollary, which matters just as much: where a number CAN be computed, it
must also carry how. WUE is integrated off the tower makeup meters, CUE is PUE
times a published grid factor, and outdoor humidity is derived from the
dry/wet bulb pair - three different kinds of claim, and each says which it is
in its own `method` and `note`. Cooling headroom is DELEGATED to the cooling
service rather than computed twice: that module owns nameplate, staging and
what counts as a machine, and two answers to "how full is the plant" differing
by which chillers each counted is how a hall gets promised capacity that is not
there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ashrae
from app.core.alert_taxonomy import ALARM, CATEGORIES, DETECTIONS
from app.repositories import sites as repo
from app.services import cooling as cooling_service
from app.services import pue as pue_service


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(part: Any, whole: Any) -> float | None:
    p, w = _f(part), _f(whole)
    if p is None or w is None or w <= 0:
        return None
    return round(p / w * 100.0, 1)


def _alarms(row: dict[str, Any]) -> dict[str, Any]:
    """The alarm block every row and the strip share.

    Two numbers that must never be mistaken for each other:

    * `total` is ALARMS - conditions requiring a response now. The ALARMS
      counter and the ALM column read this, and `critical`/`major`/`minor`
      describe the same population.
    * `by_category` is EVERY open condition in that domain, alarms and alerts
      together, because "how much is going on in the plant" is the question a
      domain counter answers. `by_category_alarms` is the actionable subset of
      each, so a tile can be coloured by whether anything in it must be
      answered and a tooltip can say both numbers.

    They do not sum to each other and are never displayed as if they did.
    `open_total` is what the categories DO sum to.

    `by_category` is the taxonomy of docs/18-alert-taxonomy.md: one axis, the
    domain of the failing thing, which is the same as who owns the first five
    minutes. `by_detection` sits beside it rather than inside it - how a
    condition was found is an attribute, so "only what analysis noticed" is a
    filter across all eight categories instead of being a ninth.
    """
    return {
        "total": int(row.get("alerts_total") or 0),
        "critical": int(row.get("crit") or 0),
        "major": int(row.get("major") or 0),
        "minor": int(row.get("minor") or 0),
        "by_category": {c: int(row.get(f"alerts_{c}") or 0) for c in CATEGORIES},
        "by_detection": {d: int(row.get(f"detected_{d}") or 0) for d in DETECTIONS},
        "by_category_alarms": {
            c: int(row.get(f"alarms_{c}") or 0) for c in CATEGORIES
        },
        "open_total": int(row.get("open_total") or 0),
    }


def _rh_absent_reason(dry: float | None, wet: float | None) -> str | None:
    """Why humidity could not be derived - never a bare blank.

    The two cases are different problems. No wet bulb is missing
    instrumentation; a wet bulb ABOVE the dry bulb is a fault, because
    evaporation can only depress it, and saying so on the tile is how a
    swapped or dry-wicked sensor gets noticed instead of quietly zeroing a
    sustainability figure.
    """
    if dry is None or wet is None:
        return "no hygrometer, and no dry/wet bulb pair to derive from"
    return ("wet bulb reads above dry bulb - the pair cannot be believed, so "
            "no humidity is derived from it")


def _weather(rows: dict[str, Any]) -> dict[str, Any]:
    """Shape the outdoor-air block, including its own staleness.

    `age_s` travels with the values because these points are slow-polled and a
    tower can be staged off. A reader must be able to tell "17.2 C now" from
    "17.2 C ninety minutes ago", and the drawer greys the section on the
    difference.
    """
    dry = rows.get("outdoor_dry_bulb_temp")
    wet = rows.get("outdoor_wet_bulb_temp")
    newest = max((r["ts"] for r in (dry, wet) if r and r.get("ts")), default=None)
    age = (datetime.now(UTC) - newest).total_seconds() if newest else None
    rh = (ashrae.rh_from_wet_bulb(_f(dry["value"]), _f(wet["value"]))
          if dry and wet and _f(dry["value"]) is not None
          and _f(wet["value"]) is not None else None)
    return {
        "available": bool(dry or wet),
        "note": (None if (dry or wet) else
                 "no cooling tower at this site is reporting outdoor air"),
        "dry_bulb_c": _f(dry["value"]) if dry else None,
        "wet_bulb_c": _f(wet["value"]) if wet else None,
        # DERIVED, and flagged as such all the way to the tile. No site here
        # has a hygrometer; what it has is the pair of thermometers a
        # psychrometer is made of, and the evaporative depression between them
        # carries the moisture. A BMS graphic shows RH from exactly this.
        "humidity_pct": (round(rh, 1) if rh is not None else None),
        "humidity_derived": rh is not None,
        "humidity_note": (
            "derived from dry/wet bulb at an assumed 1013 hPa - no hygrometer "
            "at this site" if rh is not None else
            _rh_absent_reason(_f(dry["value"]) if dry else None,
                              _f(wet["value"]) if wet else None)),
        # No source at all: nothing on a cooling tower reads wind.
        "wind_speed_ms": None,
        "source": "cooling tower controller (BACnet)" if (dry or wet) else None,
        "as_of": newest,
        "age_s": round(age, 1) if age is not None else None,
    }


# How stale the newest sample may be before the page stops vouching for its
# own numbers. Poll intervals across the estate run to 60 s, and a collector may
# be a cycle late without anything being wrong, so this is deliberately several
# cycles rather than a tight bound - a trust warning that cries wolf is one
# nobody reads.
_TELEMETRY_TRUSTED_S = 300.0


async def platform_health(session: AsyncSession) -> dict[str, Any]:
    """The state of the monitoring, kept out of the estate's counters.

    Two things, and they are different claims:

    * the open platform conditions - what is broken in the pipeline;
    * the age of the newest sample - what that has done to every other number
      on the page.

    The second is the one that matters to a reader. A stalled ingest worker is
    the platform team's problem; telemetry that is nine minutes old is
    everybody's, because it means the thermal map they are looking at is nine
    minutes old too.

    `state` is the badge: ok, degraded (something informational), impaired
    (an alarm), blind (a critical alarm, or telemetry that has stopped).
    """
    conditions = await repo.platform_conditions(session)
    age_s = await repo.telemetry_age_seconds(session)

    alarms = [c for c in conditions if c["response_class"] == ALARM]
    worst = conditions[0]["severity"] if conditions else None
    stale = age_s is not None and age_s > _TELEMETRY_TRUSTED_S

    if worst == "CRITICAL" or age_s is None or stale:
        state = "blind"
    elif alarms:
        state = "impaired"
    elif conditions:
        state = "degraded"
    else:
        state = "ok"

    return {
        "state": state,
        "alarms": len(alarms),
        "alerts": len(conditions) - len(alarms),
        "telemetry_age_s": round(age_s, 1) if age_s is not None else None,
        # The threshold travels with the number: a UI that hard-codes its own
        # idea of "stale" will disagree with the badge sooner or later.
        "telemetry_trusted_s": _TELEMETRY_TRUSTED_S,
        "telemetry_stale": stale or age_s is None,
        "conditions": [{
            "alarm_type": c["alarm_type"],
            "instance": c["instance"],
            "severity": c["severity"],
            "response_class": c["response_class"],
            "message": c["message"],
            "first_seen": c["first_seen"],
        } for c in conditions],
    }


async def overview(session: AsyncSession) -> dict[str, Any]:
    """Everything the home page table and alert strip need, in one request."""
    sites = await repo.site_rollups(session)
    rooms = await repo.room_rollups(session)
    totals = await repo.fleet_alert_totals(session)
    platform = await platform_health(session)

    by_site: dict[str, list[dict[str, Any]]] = {}
    for r in rooms:
        by_site.setdefault(r["datacenter_id"], []).append({
            "id": r["id"],
            "name": r["name"],
            "room_type": r["room_type"],
            # White space or facility. The home table hides facility rooms by
            # default: nobody racks a server in a generator hall, and eight
            # rows of dashes made the halls harder to find.
            "room_class": r["room_class"],
            "floor": r["floor"],
            "datacenter_id": r["datacenter_id"],
            "datacenter_code": r["datacenter_code"],
            "rack_count": int(r["rack_count"] or 0),
            "device_count": int(r["device_count"] or 0),
            "offline_count": int(r["offline_count"] or 0),
            "alarms": _alarms(r),
        })

    return {
        "sites": [{
            "id": s["id"],
            "code": s["code"],
            "name": s["name"],
            # Unseeded in the current dataset. Left null rather than filled with
            # the datacenter code so the UI can say "not set" instead of
            # implying a location it does not know.
            "city": s["city"],
            "country": s["country"],
            "timezone": s["timezone"],
            "room_count": int(s["room_count"] or 0),
            "device_count": int(s["device_count"] or 0),
            "online_count": int(s["online_count"] or 0),
            "offline_count": int(s["offline_count"] or 0),
            "alarms": _alarms(s),
            "rooms": by_site.get(s["id"], []),
        } for s in sites],
        "totals": _alarms(totals),
        # Not part of the estate arithmetic above: the state of the monitoring
        # itself. See `platform_health`.
        "platform": platform,
        "as_of": datetime.now(UTC),
    }


def _pue_detail(pue: dict[str, Any]) -> str | None:
    """The measurement's own provenance, for the hover rather than the tile.

    Everything here is true and none of it is worth a line of the panel until
    somebody doubts the number - at which point all of it is the first thing
    they ask for.
    """
    bits: list[str] = []
    point = pue.get("measurement_point")
    if point:
        bits.append(f"IT measured at the {point}")
    if pue.get("total_facility_kwh") is not None:
        bits.append(f"{pue['total_facility_kwh']:.0f} kWh facility over "
                    f"{pue.get('it_kwh', 0):.0f} kWh IT")
    meters = pue.get("meters") or {}
    if meters:
        bits.append(f"{meters.get('facility', 0)} facility meter(s), "
                    f"{meters.get('it', 0)} IT")
    if pue.get("counter_resets"):
        bits.append(f"{pue['counter_resets']} counter reset(s) in the window")
    return " · ".join(bits) or None


# A WUE this large means the denominator is small, not that the towers are
# leaking: fixed evaporation from a basin does not scale down with IT load, so
# a site running at 14 % of design reports a worse ratio than the same plant
# fully loaded. Worth saying on the tile, because the opposite reading - "our
# water efficiency collapsed" - is the one a reader reaches for.
WUE_HIGH_L_PER_KWH = 3.0


def _wue(water: dict[str, Any], it_kwh: float | None) -> dict[str, Any]:
    """Water Usage Effectiveness: litres of site water per IT kWh.

    Green Grid WUE is a SITE ratio - all water consumed by the facility over
    IT energy. On this fleet the only metered water crossing the boundary is
    cooling-tower makeup, which is the dominant term at any evaporatively
    cooled site but is not the whole of it: humidification, domestic supply
    and the water embedded in purchased chilled water are all outside what is
    instrumented here. The value is labelled by what it actually counted so
    nobody reads it as a full site WUE.
    """
    litres = float(water.get("litres") or 0.0)
    if it_kwh is None or it_kwh <= 0:
        return {"value": None, "method": None, "detail": None,
                "note": "IT energy for the window is unavailable, so there is "
                        "nothing to divide the metered water by"}
    if not water.get("samples"):
        return {"value": None, "method": None, "detail": None,
                "note": "no makeup-water meter reported in the window"}

    value = litres / it_kwh
    # What the tile says, and what it says only when asked. The split is by
    # WHO NEEDS IT: the scope caveat changes how the number should be read and
    # stays visible; the arithmetic behind it is for somebody checking the
    # number and belongs in the hover.
    detail = [f"{litres:.0f} L makeup over {it_kwh:.0f} kWh IT, "
              f"{water.get('towers', 0)} tower(s), 1 h"]
    # The scope is already on the tile in `method` ("tower makeup,
    # flow-integrated"), so the note carries only what that does not say: the
    # caveats that change how THIS reading should be read. Repeating the scope
    # here printed "tower makeup, flow-integrated - tower makeup only".
    caveats: list[str] = []
    if water.get("gaps"):
        # Stays VISIBLE. A gap is water the integration did not count, so the
        # ratio is a floor, and a reader comparing it with last week has to
        # know the meter went quiet rather than the plant got thrifty.
        caveats.append(f"lower bound, {water['gaps']} gap(s) excluded")
        detail.append(f"gaps longer than {repo.MAKEUP_GAP_MAX_S:.0f} s are not "
                      "integrated across - a tower staged off did not keep "
                      "drawing water")
    if value > WUE_HIGH_L_PER_KWH:
        caveats.append("high on low IT load")
        detail.append("basin evaporation is largely fixed, so the ratio rises "
                      "as IT load falls - this is not a leak")
    detail.append("counts cooling-tower makeup only, not humidification, "
                  "domestic supply or water embedded in purchased chilled "
                  "water, so it is not a full Green Grid site WUE")
    return {"value": round(value, 3), "method": "tower makeup, flow-integrated",
            "note": " · ".join(caveats) or None,
            "detail": " · ".join(detail)}


def _cue(pue: dict[str, Any], dc: dict[str, Any]) -> dict[str, Any]:
    """Carbon Usage Effectiveness: kg CO2e per IT kWh.

    CUE is total facility CO2e over IT energy, and since every kWh here comes
    off one grid connection that reduces exactly to PUE x the grid emission
    factor. The factor is not measurable at the site - it is published, it
    lives on the datacenter row, and migration 0073 explains why.

    The factor's provenance travels with the value in every case. A carbon
    figure whose source is not on the page beside it is the kind of number
    that ends up in a report nobody can defend.
    """
    factor = _f((dc.get("attributes") or {}).get("grid_carbon_kg_per_kwh"))
    basis = (dc.get("attributes") or {}).get("grid_carbon_basis")
    value = _f(pue.get("pue"))
    if factor is None:
        return {"value": None, "method": None, "detail": None,
                "note": "no grid carbon intensity is set for this site - it is "
                        "published data, not something the site can meter"}
    if value is None:
        return {"value": None, "method": None, "detail": None,
                "note": "PUE is unavailable for the window, and CUE is PUE "
                        "times the grid factor"}
    # Scope 2 only, and said so: on-site diesel burned during a utility outage
    # or a generator test is scope 1 and is not in this number. A site that ran
    # its generators all month would under-report here.
    return {"value": round(value * factor, 3),
            "method": "PUE x published grid factor",
            # Visible: the factor and where it came from. An unattributed
            # carbon number is worse than none, so the source never moves to
            # the hover - only the scope caveat does.
            "note": f"{factor:.3f} kg CO2e/kWh · "
                    f"{basis or 'factor source not recorded'}",
            "detail": ("grid electricity only: scope 2. Diesel burnt during an "
                       "outage or a generator test is scope 1 and is not in "
                       "this figure, so a month spent on generators would "
                       "under-report · an annual published factor cannot "
                       "distinguish 3 a.m. wind from 6 p.m. gas; an hourly "
                       "feed would")}


async def kpi(session: AsyncSession, datacenter_id: str) -> dict[str, Any] | None:
    """The site KPI drawer: efficiency, load, utilisation, alerts."""
    dc = await repo.datacenter(session, datacenter_id)
    if dc is None:
        return None

    power = await repo.site_power(session, datacenter_id)
    space = await repo.site_space(session, datacenter_id)
    devices = await repo.site_devices(session, datacenter_id)
    endpoints = await repo.site_endpoints(session, datacenter_id)
    alarms = await repo.site_alarms(session, datacenter_id)
    weather = await repo.site_weather(session, datacenter_id)

    it_kw = _f(power.get("it_load_kw")) or 0.0
    cooling_kw = _f(power.get("cooling_load_kw")) or 0.0
    other_kw = _f(power.get("facility_other_kw")) or 0.0
    total_kw = it_kw + cooling_kw + other_kw

    end = datetime.now(UTC)
    pue = await pue_service.compute(session, start=end - timedelta(hours=1),
                                    end=end, datacenter_id=datacenter_id)

    # Cooling Effectiveness Ratio: facility cooling kW per IT kW. Falls
    # straight out of the same two sums PUE uses, and unlike PUE it isolates
    # the cooling plant from the rest of the facility load.
    cer = round(cooling_kw / it_kw, 3) if it_kw > 0 else None

    # WUE and CUE ride on the SAME window and the SAME IT energy denominator as
    # PUE. That is not tidiness: three efficiency ratios on one panel that were
    # measured over different periods invite arithmetic between them that does
    # not hold, and PUE x carbon-factor IS how CUE is defined.
    it_kwh = _f(pue.get("it_kwh"))
    water = await repo.site_makeup_water(session, datacenter_id,
                                         start=end - timedelta(hours=1), end=end)
    # ASK the cooling service rather than recompute: it owns nameplate, staging
    # and what counts as a machine, and a second implementation here would
    # disagree with /cooling the first time either changed.
    plant = await cooling_service.plant_view(session, datacenter_id=datacenter_id)
    wue = _wue(water, it_kwh)
    cue = _cue(pue, dc)

    return {
        "site": {
            "id": dc["id"],
            "code": dc["code"],
            "name": dc["name"],
            "city": dc["city"],
            "country": dc["country"],
            "timezone": dc["timezone"],
            "design_it_kw": _f(dc["design_it_kw"]),
            "design_pue": _f(dc["design_pue"]),
        },
        "monitored": {
            "devices": int(devices.get("total") or 0),
            "devices_online": int(devices.get("online") or 0),
            "devices_offline": int(devices.get("offline") or 0),
            "endpoints": int(endpoints.get("total") or 0),
            "endpoints_enabled": int(endpoints.get("enabled") or 0),
            "protocols": int(endpoints.get("protocols") or 0),
            "racks": int(space.get("rack_count") or 0),
        },
        "efficiency": {
            "pue": {
                "value": pue.get("pue"),
                "method": pue.get("method"),
                "category": pue.get("category"),
                "note": pue.get("note"),
                "detail": _pue_detail(pue),
                # The design figure to read the measurement against, and the
                # health of the measurement itself. They are different
                # judgements and the tile keeps them apart: `plausible` is
                # whether this number can be true at all (below 1.0 it cannot),
                # `target` is whether it is good.
                "plausible": pue.get("plausible"),
                "target": _f(dc.get("design_pue")),
                "target_label": "design",
                # NOT like for like, and the panel has to say so rather than
                # print a flattering gap. See migration 0074: the design figure
                # is a cooling-only anchor, the measurement is Category 1 and
                # carries distribution losses the anchor never counted.
                "target_note": (
                    (dc.get("attributes") or {}).get("design_pue_basis")
                    if dc.get("design_pue") else None),
            },
            "cer": {"value": cer,
                    "note": None if cer is not None
                    else "no IT load is reporting",
                    "detail": ("facility cooling kW per IT kW. Unlike PUE it "
                               "isolates the cooling plant from the rest of "
                               "the facility load"
                               if cer is not None else None)},
            "wue": wue,
            "cue": cue,
        },
        "power": {
            "total_kw": round(total_kw, 1),
            "it_load_kw": round(it_kw, 1),
            "cooling_kw": round(cooling_kw, 1),
            "facility_other_kw": round(other_kw, 1),
            "reporting_devices": int(power.get("reporting_devices") or 0),
        },
        "utilisation": {
            "power": {
                "pct": _pct(it_kw, dc["design_it_kw"]),
                "basis": (f"of {_f(dc['design_it_kw']):.0f} kW design IT"
                          if dc["design_it_kw"] else None),
                # How the denominator was arrived at. It is DERIVED from the
                # installed UPS, not typed in by a facilities engineer, and a
                # capacity percentage whose denominator cannot be explained is
                # one nobody should plan against.
                "note": ((dc.get("attributes") or {}).get("design_it_kw_basis")
                         if dc["design_it_kw"] else
                         "no UPS in inventory carries a nameplate, so the "
                         "design IT load cannot be derived"),
            },
            "space": {
                "pct": _pct(space.get("used_u"), space.get("total_u")),
                "basis": f"{int(space.get('used_u') or 0)} of "
                         f"{int(space.get('total_u') or 0)} U across "
                         f"{int(space.get('rack_count') or 0)} racks",
                "note": None,
            },
            # Computed BY the cooling service, not here. Note it answers a
            # different question from the staging figure on /cooling: this is
            # load against installed capacity with the N+1 machine reserved -
            # room to grow - where /cooling divides by what is running, which
            # is whether the plant is staged correctly now.
            "cooling": cooling_service.headroom(plant["plant"]),
        },
        # Outdoor air, read off the cooling-tower controllers over BACnet.
        #
        # Dry bulb and wet bulb only. Humidity and wind are not reported: the
        # site has no weather station, and deriving relative humidity from a
        # dry/wet bulb pair would publish a psychrometric calculation as though
        # it were an instrument reading. Wind has no source at all.
        "weather": _weather(weather),
        "alarms": _alarms(alarms),
        "as_of": datetime.now(UTC),
    }
