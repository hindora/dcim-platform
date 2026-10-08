"""Official hazard warnings in effect near each site.

A NOC does not forecast weather; it subscribes to the people who issue the
warnings and asks one question - is anything official in effect over my
sites? Two public feeds answer it here:

* **US National Weather Service** (api.weather.gov): every watch, warning and
  advisory whose zone contains the site - flood, flash flood, severe
  thunderstorm, tornado, tropical storm and hurricane, red-flag fire weather,
  excessive heat, winter storm, high wind. Point queries outside the United
  States are refused by the service, which is reported as "not covered"
  rather than as an error.
* **GDACS**, the UN/EC Global Disaster Alert and Coordination System: Orange
  and Red events worldwide - floods, tropical cyclones, wildfires,
  earthquakes, volcanoes - as points with a magnitude. An event is attached
  to a site when it lies within a radius that depends on the hazard: a
  cyclone matters from much further away than a wildfire.

Both are fetched by the platform, not the browser: GDACS does not permit
cross-origin reads, NWS wants a named user agent, and an air-gapped install
should see one sentence from one place. Responses are cached for five
minutes; the feeds themselves update no faster.

This is a read of somebody else's judgement. Nothing here is derived from the
estate's own sensors, and nothing is raised as a platform alarm - a warning
is context for the operator, not a fault in the plant.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.repositories import sites as repo

NWS_ALERTS = "https://api.weather.gov/alerts/active"
GDACS_EVENTS = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
                "?eventlist=EQ;TC;FL;VO;WF&alertlevel=Orange;Red")
USER_AGENT = "dcim-platform world map (https://github.com/hindora/dcim-platform)"
CACHE_TTL_S = 300.0
TIMEOUT_S = 10.0

SEVERITY_ORDER = {"extreme": 0, "severe": 1, "moderate": 2, "minor": 3}
NWS_SEVERITY = {"Extreme": "extreme", "Severe": "severe", "Moderate": "moderate", "Minor": "minor"}
GDACS_SEVERITY = {"Red": "severe", "Orange": "moderate"}
GDACS_KIND = {"FL": "flood", "TC": "cyclone", "WF": "fire", "EQ": "earthquake", "VO": "volcano"}
# How far away an event still counts for a site, by hazard. A cyclone's wind
# and rain field is hundreds of kilometres across; a wildfire's smoke and
# utility risk is local.
GDACS_RADIUS_KM = {"TC": 500.0, "FL": 250.0, "EQ": 300.0, "WF": 150.0, "VO": 150.0}

Fetch = Callable[[httpx.AsyncClient, str], Awaitable[tuple[int | None, Any]]]

# url -> (fetched_at, status, body)
_cache: dict[str, tuple[float, int | None, Any]] = {}


def kind_of(event: str) -> str:
    """Bucket an NWS event name so the UI can pick a glyph and a colour."""
    e = event.lower()
    if "tornado" in e:
        return "tornado"
    if "hurricane" in e or "tropical" in e or "typhoon" in e:
        return "cyclone"
    if "flood" in e:
        return "flood"
    if "thunderstorm" in e:
        return "storm"
    if "fire" in e or "red flag" in e:
        return "fire"
    if "heat" in e:
        return "heat"
    if any(w in e for w in ("winter", "snow", "ice", "blizzard", "freez", "cold", "wind chill")):
        return "winter"
    if "wind" in e or "gale" in e:
        return "wind"
    if "rain" in e or "hydrologic" in e:
        return "rain"
    return "other"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


async def fetch_json(client: httpx.AsyncClient, url: str) -> tuple[int | None, Any]:
    """(status, body); (None, error text) when the request itself failed.

    Cached for CACHE_TTL_S, including failures - a dead feed is not retried on
    every wall-display refresh.
    """
    hit = _cache.get(url)
    now = time.monotonic()
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1], hit[2]
    try:
        r = await client.get(url)
        body: Any = r.json() if r.content else None
        out: tuple[int | None, Any] = (r.status_code, body)
    except (httpx.HTTPError, ValueError) as exc:
        out = (None, f"{type(exc).__name__}: {exc}")
    _cache[url] = (now, out[0], out[1])
    return out


def _nws_alerts(body: Any) -> list[dict[str, Any]]:
    out = []
    for f in (body or {}).get("features", []) or []:
        p = f.get("properties") or {}
        if p.get("status", "Actual") != "Actual":
            continue
        event = p.get("event") or "Weather alert"
        out.append({
            "id": p.get("id") or f.get("id"),
            "source": "nws",
            "kind": kind_of(event),
            "event": event,
            "severity": NWS_SEVERITY.get(p.get("severity"), "minor"),
            "headline": p.get("headline"),
            "area": p.get("areaDesc"),
            "onset": p.get("onset") or p.get("effective"),
            "ends": p.get("ends") or p.get("expires"),
            "issuer": p.get("senderName"),
            "url": None,
            "distance_km": None,
        })
    return out


def _gdacs_alerts(body: Any, lat: float, lon: float) -> list[dict[str, Any]]:
    out = []
    for f in (body or {}).get("features", []) or []:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        if str(p.get("iscurrent", "")).lower() != "true" or g.get("type") != "Point":
            continue
        et = p.get("eventtype")
        if et not in GDACS_KIND:
            continue
        elon, elat = g["coordinates"][:2]
        d = haversine_km(lat, lon, float(elat), float(elon))
        if d > GDACS_RADIUS_KM[et]:
            continue
        sev = p.get("severitydata") or {}
        out.append({
            "id": f"gdacs-{et}-{p.get('eventid')}",
            "source": "gdacs",
            "kind": GDACS_KIND[et],
            "event": p.get("name") or GDACS_KIND[et].title(),
            "severity": GDACS_SEVERITY.get(p.get("alertlevel"), "moderate"),
            "headline": sev.get("severitytext"),
            "area": p.get("country"),
            "onset": p.get("fromdate"),
            "ends": p.get("todate"),
            "issuer": "GDACS",
            "url": (p.get("url") or {}).get("report"),
            "distance_km": round(d),
        })
    return out


def _sort(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(alerts, key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a.get("onset") or ""))


async def site_hazards(session: AsyncSession, fetch: Fetch = fetch_json) -> dict[str, Any]:
    """Warnings per placed site, and the health of the feeds they came from."""
    now = datetime.now(UTC)
    sites = [s for s in await repo.site_rollups(session)
             if s.get("latitude") is not None and s.get("longitude") is not None]
    if not get_settings().hazard_feeds_enabled:
        return {"available": False, "as_of": now, "sources": [], "sites": {},
                "note": "official warning feeds are switched off (DCIM_HAZARD_FEEDS_ENABLED)"}

    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True,
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/geo+json, application/json"}) as client:
        results = await asyncio.gather(
            fetch(client, GDACS_EVENTS),
            *(fetch(client, f"{NWS_ALERTS}?point={float(s['latitude']):.4f},{float(s['longitude']):.4f}")
              for s in sites))
    gdacs_status, gdacs_body = results[0]
    gdacs_ok = gdacs_status == 200 and isinstance(gdacs_body, dict)

    per_site: dict[str, Any] = {}
    nws_errors = 0
    for s, (status, body) in zip(sites, results[1:], strict=True):
        lat, lon = float(s["latitude"]), float(s["longitude"])
        alerts: list[dict[str, Any]] = []
        covered = status == 200 and isinstance(body, dict)
        if covered:
            alerts += _nws_alerts(body)
        elif status != 400:          # 400 is NWS saying "not my country"
            nws_errors += 1
        if gdacs_ok:
            alerts += _gdacs_alerts(gdacs_body, lat, lon)
        per_site[s["id"]] = {"covered": covered, "alerts": _sort(alerts)}

    nws_ok = bool(sites) and nws_errors == 0
    sources = [
        {"id": "nws", "name": "US National Weather Service", "ok": nws_ok,
         "note": None if nws_ok else f"{nws_errors} of {len(sites)} point queries failed"},
        {"id": "gdacs", "name": "GDACS (UN / European Commission)", "ok": gdacs_ok,
         "note": None if gdacs_ok else (f"HTTP {gdacs_status}" if gdacs_status else str(gdacs_body)[:160])},
    ]
    available = nws_ok or gdacs_ok
    return {
        "available": available,
        "note": None if available else "neither warning feed could be reached from the platform host",
        "as_of": now,
        "sources": sources,
        "sites": per_site,
    }
