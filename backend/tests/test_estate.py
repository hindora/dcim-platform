"""The estate pages: folding, absence, and the arithmetic that must not drift.

These exercise the shaping layer against fabricated repository rows. The SQL
has its own proof - it either runs or it does not - but the folding does not:
a site average that quietly becomes a mean-of-means still returns a plausible
number, and nothing downstream would ever notice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import estate


class _FakeSession:
    """Stands in for AsyncSession. The service never touches it directly."""


def _room(room_id: str, dc: str, code: str, *, f_sum=None, f_n=0, f_max=None,
          f_in_band=0, c_sum=None, c_n=0, c_max=None, name=None,
          room_class="white_space"):
    return {
        "room_id": room_id, "room_name": name or f"room-{room_id}",
        "floor": "1", "room_type": "data_hall", "room_class": room_class,
        "datacenter_id": dc,
        "site_code": code, "site_name": code, "rack_count": 2,
        "f_sum": f_sum, "f_n": f_n, "f_max": f_max, "f_in_band": f_in_band,
        "c_sum": c_sum, "c_n": c_n, "c_max": c_max,
    }


@pytest.mark.asyncio
async def test_site_average_is_weighted_by_readings(monkeypatch):
    """A busy sensor must outweigh a quiet one.

    Room A contributes 900 readings averaging 20 C; room B contributes 100
    averaging 30 C. The site is 21.0, not the 25.0 a mean-of-means would give.
    """
    rows = [
        _room("a", "dc1", "DC1", f_sum=18000.0, f_n=900, f_max=22.0, f_in_band=900),
        _room("b", "dc1", "DC1", f_sum=3000.0, f_n=100, f_max=31.0, f_in_band=0),
    ]
    monkeypatch.setattr(estate.repo, "thermal", _returns(rows))

    out = await estate.thermal(_FakeSession(), mode="live")
    site = out["sites"][0]
    assert site["avg_c"] == 21.0
    assert site["max_c"] == 31.0
    assert site["compliance_pct"] == 90.0
    assert site["samples"] == 1000


@pytest.mark.asyncio
async def test_a_room_with_no_sensor_is_absent_not_zero(monkeypatch):
    """The distinction the whole page rests on: no reading is not a cold room."""
    monkeypatch.setattr(estate.repo, "thermal",
                        _returns([_room("a", "dc1", "DC1")]))

    out = await estate.thermal(_FakeSession(), mode="live")
    room = out["rooms"][0]
    assert room["avg_c"] is None
    assert room["compliance_pct"] is None
    assert room["note"]
    assert out["totals"]["rooms_reporting"] == 0


@pytest.mark.asyncio
async def test_delta_is_none_without_a_comparison_window(monkeypatch):
    """"Unchanged" and "nothing to compare with" are different answers."""
    monkeypatch.setattr(estate.repo, "thermal", _returns([
        _room("a", "dc1", "DC1", f_sum=200.0, f_n=10, f_max=21.0, f_in_band=10),
    ]))
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["delta_avg"] is None

    monkeypatch.setattr(estate.repo, "thermal", _returns([
        _room("a", "dc1", "DC1", f_sum=200.0, f_n=10, f_max=21.0, f_in_band=10,
              c_sum=190.0, c_n=10, c_max=20.0),
    ]))
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["delta_avg"] == 1.0


@pytest.mark.asyncio
async def test_compliance_counts_readings_inside_the_band(monkeypatch):
    monkeypatch.setattr(estate.repo, "thermal", _returns([
        _room("a", "dc1", "DC1", f_sum=800.0, f_n=40, f_max=29.0, f_in_band=30),
    ]))
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["compliance_pct"] == 75.0
    assert out["band"]["low_c"] == estate.BAND_LOW_C
    assert out["band"]["high_c"] == estate.BAND_HIGH_C


def _power_row(room_id: str, dc: str, code: str, *, avg_it=None, peak_it=None,
               avg_cooling=None, peak_cooling=None, avg_total=None,
               peak_total=None, prev_total=None, room_class="white_space"):
    return {
        "room_id": room_id, "room_name": f"room-{room_id}", "floor": None,
        "room_class": room_class,
        "datacenter_id": dc, "site_code": code, "site_name": code,
        "avg_it": avg_it, "peak_it": peak_it, "avg_cooling": avg_cooling,
        "peak_cooling": peak_cooling, "avg_other": 0.0, "peak_other": 0.0,
        "avg_total": avg_total, "peak_total": peak_total, "buckets": 12,
        "prev_total": prev_total, "prev_buckets": 12,
    }


@pytest.mark.asyncio
async def test_peak_mode_reads_the_peak_columns(monkeypatch):
    """Average and peak are different columns, not the same number rounded."""
    rows = [_power_row("a", "dc1", "DC1", avg_it=100.0, peak_it=180.0,
                       avg_cooling=40.0, peak_cooling=60.0,
                       avg_total=140.0, peak_total=240.0)]
    monkeypatch.setattr(estate.repo, "power_window", _returns(rows))

    avg = await estate.power(_FakeSession(), mode="average")
    peak = await estate.power(_FakeSession(), mode="peak")
    assert avg["rooms"][0]["total_kw"] == 140.0
    assert peak["rooms"][0]["total_kw"] == 240.0
    # PUE follows the mode it was asked for rather than mixing the two.
    assert peak["rooms"][0]["pue"] == round(240.0 / 180.0, 3)


@pytest.mark.asyncio
async def test_dc_power_is_never_invented(monkeypatch):
    monkeypatch.setattr(estate.repo, "power_live", _returns([
        _power_row("a", "dc1", "DC1", avg_it=10.0, avg_cooling=5.0),
    ]))
    out = await estate.power(_FakeSession(), live=True)
    assert out["rooms"][0]["it_dc_kw"] is None
    assert out["totals"]["it_dc_kw"] is None


@pytest.mark.asyncio
async def test_pue_needs_an_it_load_to_divide_by(monkeypatch):
    """A plant room draws cooling power and hosts no IT. Its PUE is not 0."""
    monkeypatch.setattr(estate.repo, "power_live", _returns([
        _power_row("plant", "dc1", "DC1", avg_it=0.0, avg_cooling=29.0),
    ]))
    out = await estate.power(_FakeSession(), live=True)
    assert out["rooms"][0]["pue"] is None


def _util_row(room_id: str, dc: str, code: str, **over):
    row = {
        "room_id": room_id, "room_name": f"room-{room_id}", "floor": None,
        "room_class": "white_space",
        "datacenter_id": dc, "site_code": code, "site_name": code,
        "design_it_kw": None, "designed_racks": 40, "width_m": 8.4,
        "depth_m": 12.3, "rack_count": 10, "total_u": 420.0, "used_u": 210.0,
        "it_kw": 100.0, "cooling_kw": 20.0, "supply_rated_kw": None,
        "supply_units": 0, "cooling_capacity_kw": 400.0, "cooling_units": 4,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_utilisation_states_which_denominator_it_used(monkeypatch):
    monkeypatch.setattr(estate.repo, "utilisation", _returns([
        _util_row("designed", "dc1", "DC1", design_it_kw=200.0),
        _util_row("nameplate", "dc1", "DC1", supply_rated_kw=500.0, supply_units=2),
        _util_row("neither", "dc1", "DC1"),
    ]))
    monkeypatch.setattr(estate.repo, "site_design", _returns({}))

    out = await estate.utilisation(_FakeSession())
    by_id = {r["id"]: r for r in out["rooms"]}

    assert by_id["designed"]["power_pct"] == 50.0
    assert "design" in by_id["designed"]["power_basis"]

    assert by_id["nameplate"]["power_pct"] == 20.0
    # The caveat has to travel with the number: installed is not usable on a
    # 2N floor, and a reader taking 20% as headroom would be wrong by half.
    assert "not de-rated" in by_id["nameplate"]["power_basis"]

    assert by_id["neither"]["power_pct"] is None
    assert by_id["neither"]["power_basis"]

    # Space is exact everywhere - it comes from inventory, not from telemetry.
    assert by_id["neither"]["space_pct"] == 50.0


@pytest.mark.asyncio
async def test_site_power_capacity_prefers_the_site_rating(monkeypatch):
    """Summing room ratings would count a shared UPS once per room it feeds."""
    monkeypatch.setattr(estate.repo, "utilisation", _returns([
        _util_row("a", "dc1", "DC1", supply_rated_kw=500.0, supply_units=2),
        _util_row("b", "dc1", "DC1", supply_rated_kw=500.0, supply_units=2),
    ]))
    monkeypatch.setattr(estate.repo, "site_design", _returns({"dc1": 960.0}))

    out = await estate.utilisation(_FakeSession())
    site = out["sites"][0]
    assert site["power_capacity_kw"] == 960.0
    assert site["power_pct"] == round(200.0 / 960.0 * 100, 1)


@pytest.mark.asyncio
async def test_alert_drilldown_accounts_for_unlocated_alarms(monkeypatch):
    """The modal's total must reconcile with the counter that opened it."""
    monkeypatch.setattr(estate.repo, "alarms_by_room", _returns([{
        "room_id": "r1", "room_name": "Hall A", "floor": "1",
        "datacenter_id": "dc1", "site_code": "DC1", "site_name": "DC1",
        "qty": 3, "devices": 2, "critical": 1, "major": 2,
    }]))
    monkeypatch.setattr(estate.repo, "unlocated_alarms_by_category", _returns(4))

    out = await estate.alarms(_FakeSession(), category="datapoint")
    assert out["total"] == 7
    assert out["unlocated"] == 4


@pytest.mark.asyncio
async def test_facility_power_stays_in_the_site_total(monkeypatch):
    """The rows a page hides must not change the arithmetic it does.

    Two thirds of a site's cooling draw stands in its plant room. Excluding it
    to tidy the room list would move PUE from 1.4 to 1.1 and describe a plant
    that was never built.
    """
    monkeypatch.setattr(estate.repo, "power_live", _returns([
        _power_row("hall", "dc1", "DC1", avg_it=100.0, avg_cooling=10.0),
        _power_row("plant", "dc1", "DC1", avg_it=0.0, avg_cooling=30.0,
                   room_class="facility"),
    ]))
    out = await estate.power(_FakeSession(), live=True)

    assert out["totals"]["cooling_kw"] == 40.0
    assert out["totals"]["pue"] == round(140.0 / 100.0, 3)
    # And the difference is stated, so a header that does not match the visible
    # rows explains itself on the page.
    assert out["totals"]["facility"]["rooms"] == 1
    assert out["totals"]["facility"]["cooling_kw"] == 30.0


@pytest.mark.asyncio
async def test_space_counts_white_space_only(monkeypatch):
    """A plant room's two BMS cabinets are not estate capacity."""
    monkeypatch.setattr(estate.repo, "utilisation", _returns([
        _util_row("hall", "dc1", "DC1"),
        _util_row("plant", "dc1", "DC1", room_class="facility",
                  rack_count=2, total_u=84.0, used_u=2.0, designed_racks=None,
                  width_m=None, depth_m=None),
    ]))
    monkeypatch.setattr(estate.repo, "site_design", _returns({}))
    out = await estate.utilisation(_FakeSession())

    site = out["sites"][0]
    assert site["space_total_u"] == 420.0        # the hall only
    # The rack count beside a white-space U total has to be the same subset,
    # or the row says "12 racks" over a figure drawn from 10 of them.
    assert site["rack_count"] == 10
    assert site["facility_racks"] == 2
    assert out["totals"]["facility_rooms"] == 1
    # Load is whole-estate even though the plant row is not white space.
    assert site["power_used_kw"] == 200.0


@pytest.mark.asyncio
async def test_build_out_measures_racks_against_drawn_positions(monkeypatch):
    """A hall can be 50% full by U and 25% built out. Different questions."""
    monkeypatch.setattr(estate.repo, "utilisation", _returns([
        _util_row("hall", "dc1", "DC1", rack_count=10, designed_racks=40),
    ]))
    monkeypatch.setattr(estate.repo, "site_design", _returns({}))
    out = await estate.utilisation(_FakeSession())

    room = out["rooms"][0]
    assert room["space_pct"] == 50.0
    assert room["built_out_pct"] == 25.0
    assert room["floor_area_m2"] == 103.3


def test_bucket_widens_with_the_window():
    now = datetime.now(UTC)
    assert estate._bucket_for(now - timedelta(hours=6), now) == timedelta(minutes=5)
    assert estate._bucket_for(now - timedelta(days=7), now) == timedelta(minutes=30)
    assert estate._bucket_for(now - timedelta(days=30), now) == timedelta(hours=1)
    assert estate._bucket_for(now - timedelta(days=400), now) == timedelta(days=1)


def _returns(value):
    async def _fn(*_args, **_kwargs):
        return value
    return _fn
