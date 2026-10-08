"""Official warnings are attached to the sites they are in effect over.

The feeds are faked at the fetch boundary; the shaping, the distance rule,
the "not covered" case and the degraded cases are what is pinned here.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.repositories import sites as repo
from app.services import hazards

CHICAGO = {"id": "dc1", "code": "DC1", "latitude": 41.88, "longitude": -87.63}
LONDON = {"id": "dc3", "code": "DC3", "latitude": 51.5, "longitude": -0.12}

NWS_FLOOD = {"features": [
    {"id": "a1", "properties": {"id": "urn:a1", "status": "Actual", "event": "Flood Watch", "severity": "Moderate",
                                "headline": "Flood Watch until 6 PM", "areaDesc": "Cook", "onset": "2026-10-08T10:00:00-05:00",
                                "ends": "2026-10-08T18:00:00-05:00", "senderName": "NWS Chicago IL"}},
    {"id": "a2", "properties": {"id": "urn:a2", "status": "Actual", "event": "Tornado Warning", "severity": "Extreme",
                                "headline": "Tornado Warning", "areaDesc": "Cook", "onset": "2026-10-08T12:00:00-05:00",
                                "expires": "2026-10-08T12:45:00-05:00", "senderName": "NWS Chicago IL"}},
    {"id": "a3", "properties": {"id": "urn:a3", "status": "Test", "event": "Test Message", "severity": "Minor"}},
]}

GDACS = {"features": [
    # A flood 60 km from Chicago - attached.
    {"geometry": {"type": "Point", "coordinates": [-88.3, 42.2]},
     "properties": {"eventtype": "FL", "eventid": 1, "name": "Flood in United States", "alertlevel": "Orange",
                    "iscurrent": "true", "country": "United States", "fromdate": "2026-10-07T00:00:00",
                    "todate": "2026-10-09T00:00:00", "severitydata": {"severitytext": "Medium humanitarian impact"},
                    "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1"}}},
    # A cyclone off Mexico, 3000 km away - not attached.
    {"geometry": {"type": "Point", "coordinates": [-103.1, 15.4]},
     "properties": {"eventtype": "TC", "eventid": 2, "name": "Tropical Cyclone SIMON-26", "alertlevel": "Red",
                    "iscurrent": "true", "country": "Mexico"}},
    # Over, so not current - not attached even though it is close.
    {"geometry": {"type": "Point", "coordinates": [-87.7, 41.9]},
     "properties": {"eventtype": "WF", "eventid": 3, "name": "Forest fire", "alertlevel": "Red", "iscurrent": "false"}},
]}


def _fake_fetch(nws: dict[str, tuple[int | None, Any]], gdacs: tuple[int | None, Any]):
    async def fetch(_client, url: str):
        if url.startswith(hazards.GDACS_EVENTS):
            return gdacs
        for key, value in nws.items():
            if key in url:
                return value
        return (500, None)
    return fetch


def _run(monkeypatch, sites, fetch):
    async def rollups(_s):
        return sites
    monkeypatch.setattr(repo, "site_rollups", rollups)
    return asyncio.run(hazards.site_hazards(None, fetch=fetch))


def test_warnings_are_sorted_worst_first_and_test_messages_dropped(monkeypatch):
    out = _run(monkeypatch, [CHICAGO], _fake_fetch({"41.8800,-87.6300": (200, NWS_FLOOD)}, (200, GDACS)))
    site = out["sites"]["dc1"]
    assert site["covered"] is True
    events = [a["event"] for a in site["alerts"]]
    # Two moderates: the one that began first comes first.
    assert events == ["Tornado Warning", "Flood in United States", "Flood Watch"]
    tornado = site["alerts"][0]
    assert tornado["severity"] == "extreme" and tornado["kind"] == "tornado"
    assert tornado["ends"] == "2026-10-08T12:45:00-05:00"      # expires stands in for ends
    assert out["available"] is True and all(s["ok"] for s in out["sources"])


def test_a_gdacs_event_counts_only_within_its_radius(monkeypatch):
    out = _run(monkeypatch, [CHICAGO], _fake_fetch({"41.8800": (200, {"features": []})}, (200, GDACS)))
    alerts = out["sites"]["dc1"]["alerts"]
    assert [a["event"] for a in alerts] == ["Flood in United States"]
    assert alerts[0]["source"] == "gdacs" and alerts[0]["severity"] == "moderate"
    assert 50 <= alerts[0]["distance_km"] <= 80
    assert alerts[0]["url"].startswith("https://www.gdacs.org/")


def test_outside_the_us_is_not_covered_rather_than_broken(monkeypatch):
    out = _run(monkeypatch, [LONDON], _fake_fetch({"51.5000": (400, {"title": "Invalid Parameter"})}, (200, GDACS)))
    assert out["sites"]["dc3"] == {"covered": False, "alerts": []}
    assert next(s for s in out["sources"] if s["id"] == "nws")["ok"] is True


def test_one_dead_feed_degrades_and_two_make_it_unavailable(monkeypatch):
    one = _run(monkeypatch, [CHICAGO], _fake_fetch({"41.8800": (200, NWS_FLOOD)}, (None, "ConnectError: dns")))
    assert one["available"] is True
    gdacs = next(s for s in one["sources"] if s["id"] == "gdacs")
    assert gdacs["ok"] is False and "ConnectError" in gdacs["note"]
    assert [a["event"] for a in one["sites"]["dc1"]["alerts"]] == ["Tornado Warning", "Flood Watch"]

    both = _run(monkeypatch, [CHICAGO], _fake_fetch({"41.8800": (None, "timeout")}, (503, None)))
    assert both["available"] is False and both["note"]
    assert both["sites"]["dc1"] == {"covered": False, "alerts": []}


def test_the_switch_turns_the_feeds_off(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.get_settings(), "hazard_feeds_enabled", False)
    try:
        out = _run(monkeypatch, [CHICAGO], _fake_fetch({}, (200, GDACS)))
    finally:
        monkeypatch.setattr(config.get_settings(), "hazard_feeds_enabled", True)
    assert out["available"] is False and "DCIM_HAZARD_FEEDS_ENABLED" in out["note"]


@pytest.mark.parametrize("event,kind", [
    ("Flash Flood Warning", "flood"), ("Severe Thunderstorm Watch", "storm"), ("Red Flag Warning", "fire"),
    ("Excessive Heat Warning", "heat"), ("Hurricane Warning", "cyclone"), ("Winter Storm Warning", "winter"),
    ("High Wind Warning", "wind"), ("Dense Fog Advisory", "other"),
])
def test_event_names_fall_into_their_hazard_kinds(event, kind):
    assert hazards.kind_of(event) == kind
