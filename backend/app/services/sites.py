"""Home-page site overview and the per-site KPI panel.

Two calls, not twelve. `overview()` backs the table and the alert strip;
`kpi()` backs the drawer that opens when an operator clicks KPIs on a row.

Where a number cannot be computed from what is instrumented, this returns null
WITH A REASON rather than a plausible-looking figure. A DCIM that guesses at
WUE is worse than one that admits it has no water meter, because the guess ends
up in a sustainability report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import ALARM, CATEGORIES, DETECTIONS
from app.repositories import sites as repo
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
    return {
        "available": bool(dry or wet),
        "note": (None if (dry or wet) else
                 "no cooling tower at this site is reporting outdoor air"),
        "dry_bulb_c": _f(dry["value"]) if dry else None,
        "wet_bulb_c": _f(wet["value"]) if wet else None,
        # Named so a reader cannot mistake absence for zero. Nothing at this
        # site measures either one.
        "humidity_pct": None,
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
            },
            "cer": {"value": cer, "note": None if cer is not None
                    else "no IT load is reporting"},
            # Both need instrumentation this platform does not have. Named
            # anyway so the gap is visible on the page rather than silently
            # absent from it.
            # The towers DO meter makeup water (`makeup_water_flow`, mapped off
            # the tower controller). What is missing is the integration: WUE is
            # litres per IT kWh over a window, so it needs the flow accumulated
            # against IT energy the way PUE accumulates facility energy. Until
            # that exists this stays null rather than publishing an
            # instantaneous flow rate dressed up as an efficiency ratio.
            "wue": {"value": None,
                    "note": "makeup-water flow is metered but not yet "
                            "integrated against IT energy"},
            "cue": {"value": None,
                    "note": "no grid carbon-intensity feed"},
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
            # Plant capacity lives behind /cooling, which stages chillers and
            # reasons about nameplate. Duplicating that here would give two
            # different answers for the same question.
            "cooling": {
                "pct": None, "basis": None,
                "note": "see /cooling for plant capacity against load",
            },
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
