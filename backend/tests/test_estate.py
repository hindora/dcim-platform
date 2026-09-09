"""The estate pages: folding, absence, and the arithmetic that must not drift.

These exercise the shaping layer against fabricated repository rows. The SQL
has its own proof - it either runs or it does not - but the folding does not:
a site average that quietly becomes a mean-of-means still returns a plausible
number, and nothing downstream would ever notice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.services import estate


class _FakeSession:
    """Stands in for AsyncSession. The service never touches it directly."""


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
async def test_panel_totals_reconcile_with_the_counter_that_opened_it(monkeypatch):
    """Everything open is the headline; the actionable part is stated beside it.

    Both are the ROWS, and only the rows. The counter that opens this panel
    counts located conditions, so anything that belongs to no room is reported
    beside the total rather than inside it - otherwise the panel's headline
    disagrees with the rows underneath it, which is the failure this whole area
    exists to prevent.
    """
    monkeypatch.setattr(estate.repo, "alarms_by_room", _returns([{
        "room_id": "r1", "room_name": "Hall A", "floor": "1",
        "datacenter_id": "dc1", "site_code": "DC1", "site_name": "DC1",
        "qty": 3, "alerts": 10, "devices": 2, "critical": 1, "major": 2,
    }]))
    monkeypatch.setattr(estate.repo, "unlocated_alarms_by_category",
                        _returns({"total": 4, "alarms": 1}))

    out = await estate.alarms(_FakeSession(), categories=["visibility"])
    assert out["total"] == 3 + 10
    assert out["alarms"] == 3
    assert out["unlocated"] == 4
    assert out["unlocated_alarms"] == 1


@pytest.mark.asyncio
async def test_the_row_carries_a_device_count_for_each_population(monkeypatch):
    """Two device counts, because the panel prints two populations.

    A domain panel lists alarms AND alerts, so the devices beside them must be
    the devices behind both - a room with four alerts and no alarms printing
    "0 devices" reads as a broken column. An alarms-only panel wants the other
    figure: a device that is merely warm is not a device anybody must visit.
    """
    monkeypatch.setattr(estate.repo, "alarms_by_room", _returns([{
        "room_id": "r1", "room_name": "Hall A", "floor": "1",
        "datacenter_id": "dc1", "site_code": "DC1", "site_name": "DC1",
        "qty": 0, "alerts": 4, "devices": 0, "devices_all": 3,
        "critical": 0, "major": 0,
    }]))
    monkeypatch.setattr(estate.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    row = (await estate.alarms(_FakeSession(), categories=["it_equipment"]))["rows"][0]
    assert row["devices"] == 0
    assert row["devices_all"] == 3


@pytest.mark.asyncio
async def test_a_missing_device_count_reads_zero_not_absent(monkeypatch):
    """The key must always be there: the browser sorts on it."""
    monkeypatch.setattr(estate.repo, "alarms_by_room", _returns([{
        "room_id": "r1", "room_name": "Hall A", "floor": "1",
        "datacenter_id": "dc1", "site_code": "DC1", "site_name": "DC1",
        "qty": 1, "alerts": 0, "devices": 1, "critical": 0, "major": 1,
    }]))
    monkeypatch.setattr(estate.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    row = (await estate.alarms(_FakeSession(), categories=["power"]))["rows"][0]
    assert row["devices_all"] == 0


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


# ----------------------------------------------------------------- thermal
# Everything is derived from racks; rooms are a skeleton the racks fold into.


def _room(room_id: str, dc: str, code: str, *, rack_count=2, name=None,
          room_class="white_space"):
    return {
        "room_id": room_id, "room_name": name or f"room-{room_id}",
        "floor": "1", "room_type": "data_hall", "room_class": room_class,
        "datacenter_id": dc, "site_code": code, "site_name": code,
        "rack_count": rack_count,
    }


def _rack(rack_id: str, room_id: str, *, dc="dc1", code="DC1",
          f_sum=None, f_n=0, f_max=None, f_in_band=0, f_sensors=0,
          p_sum=None, p_n=0, p_max=None, p_in_band=0, p_sensors=0,
          pc_sum=None, pc_n=0, pc_max=None,
          e_sum=None, e_n=0, c_sum=None, c_n=0, c_max=None,
          rh_sum=None, rh_n=0, rh_max=None, rh_probes=0):
    return {
        "rack_id": rack_id, "rack_name": f"R-{rack_id}", "row_name": "A",
        "u_height": 42, "room_id": room_id, "room_name": f"room-{room_id}",
        "floor": "1", "room_class": "white_space", "datacenter_id": dc,
        "site_code": code, "site_name": code,
        "f_sum": f_sum, "f_n": f_n, "f_max": f_max, "f_in_band": f_in_band,
        "f_sensors": f_sensors,
        "p_sum": p_sum, "p_n": p_n, "p_max": p_max, "p_in_band": p_in_band,
        "p_sensors": p_sensors, "pc_sum": pc_sum, "pc_n": pc_n, "pc_max": pc_max,
        "e_sum": e_sum, "e_n": e_n, "c_sum": c_sum, "c_n": c_n, "c_max": c_max,
        "rh_sum": rh_sum, "rh_n": rh_n, "rh_max": rh_max, "rh_probes": rh_probes,
    }


def _thermal(monkeypatch, rooms, racks):
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns(rooms))
    monkeypatch.setattr(estate.repo, "thermal_racks", _returns(racks))


@pytest.mark.asyncio
async def test_site_average_is_weighted_by_readings(monkeypatch):
    """A busy sensor must outweigh a quiet one.

    Room A's rack contributes 900 readings averaging 20 C; room B's contributes
    100 averaging 30 C. The site is 21.0, not the 25.0 a mean-of-means gives.
    """
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("b", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=18000.0, f_n=900, f_max=22.0, f_in_band=900),
              _rack("r2", "b", f_sum=3000.0, f_n=100, f_max=31.0, f_in_band=0)])
    out = await estate.thermal(_FakeSession(), mode="live")
    site = out["sites"][0]
    assert site["avg_c"] == 21.0
    assert site["max_c"] == 31.0
    assert site["compliance_pct"] == 90.0
    assert site["samples"] == 1000
    rooms = {r["id"]: r for r in out["rooms"]}
    assert rooms["a"]["avg_c"] == 20.0 and rooms["b"]["avg_c"] == 30.0


@pytest.mark.asyncio
async def test_a_room_with_no_sensor_is_absent_not_zero(monkeypatch):
    """The distinction the whole page rests on: no reading is not a cold room.
    A room with no racks at all is still a row - a generator hall is real."""
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("g", "dc1", "DC1", rack_count=0,
                                              room_class="facility")],
             [_rack("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live")
    rooms = {r["id"]: r for r in out["rooms"]}
    assert rooms["a"]["avg_c"] is None
    assert rooms["a"]["compliance_pct"] is None
    assert rooms["a"]["note"]
    assert rooms["g"]["avg_c"] is None and rooms["g"]["rack_count"] == 0
    assert out["totals"]["rooms_reporting"] == 0
    assert out["totals"]["rooms"] == 1 and out["totals"]["facility_rooms"] == 1


@pytest.mark.asyncio
async def test_delta_is_none_without_a_comparison_window(monkeypatch):
    """"Unchanged" and "nothing to compare with" are different answers."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=200.0, f_n=10, f_max=21.0, f_in_band=10)])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["delta_avg"] is None

    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=200.0, f_n=10, f_max=21.0, f_in_band=10,
                    c_sum=190.0, c_n=10, c_max=20.0)])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["delta_avg"] == 1.0


@pytest.mark.asyncio
async def test_compliance_counts_readings_inside_the_band(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=800.0, f_n=40, f_max=29.0, f_in_band=30)])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["compliance_pct"] == 75.0
    assert out["totals"]["compliance_pct"] == 75.0


@pytest.mark.asyncio
async def test_the_rack_probe_is_the_intake_and_the_bmc_is_the_fallback(monkeypatch):
    """A rack with a probe uses the probe even when eighteen BMCs disagree;
    a rack without one falls back to the servers; each says which."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("both", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18, f_sensors=18,
              p_sum=46.0, p_n=2, p_max=23.5, p_in_band=2, p_sensors=1,
              pc_sum=44.0, pc_n=2, pc_max=22.5, c_sum=440.0, c_n=18, c_max=25.0),
        _rack("bmc", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18, f_sensors=18),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    probe = racks["both"]
    assert probe["source"] == "probes" and probe["sensors"] == 1
    assert probe["avg_c"] == 23.0 and probe["max_c"] == 23.5 and probe["samples"] == 2
    # The delta compares probe with probe, not probe with last hour's BMCs.
    assert probe["delta_avg"] == 1.0 and probe["delta_max"] == 1.0
    bmc = racks["bmc"]
    assert bmc["source"] == "servers" and bmc["sensors"] == 18
    assert bmc["avg_c"] == 25.0
    room = out["rooms"][0]
    assert room["sources"] == {"probes": 1, "servers": 1}
    assert room["samples"] == 20
    assert "1 rack), else the servers' BMC inlet (1 rack)" in out["notes"][0]


@pytest.mark.asyncio
async def test_rack_rows_carry_delta_t_and_sensor_count(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("r1", "a", f_sum=460.0, f_n=20, f_max=24.5, f_in_band=20, f_sensors=4,
              e_sum=700.0, e_n=20, c_sum=440.0, c_n=20, c_max=23.0,
              rh_sum=100.0, rh_n=2, rh_max=52.0, rh_probes=1),
        _rack("r2", "a"),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    hot = racks["r1"]
    assert hot["kind"] == "rack" and hot["room_id"] == "a"
    assert hot["avg_c"] == 23.0 and hot["exhaust_c"] == 35.0
    assert hot["delta_t_k"] == 12.0
    assert hot["sensors"] == 4 and hot["compliance_pct"] == 100.0
    assert hot["delta_avg"] == 1.0 and hot["delta_max"] == 1.5
    assert hot["rh_avg"] == 50.0 and hot["rh_probes"] == 1
    silent = racks["r2"]
    assert silent["avg_c"] is None and silent["delta_t_k"] is None
    assert silent["sensors"] == 0 and silent["source"] is None
    assert "no intake probe or server sensor" in silent["note"]
    assert silent["delta_note"] == "no readings in the last hour"


@pytest.mark.asyncio
async def test_humidity_rides_beside_compliance_not_inside_it(monkeypatch):
    """RH is folded by readings like intake, a room with no probe reads
    absent, and the in-band share is unchanged by it."""
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("b", "dc1", "DC1"), _room("c", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=2000.0, f_n=100, f_max=22.0, f_in_band=100,
                    rh_sum=4500.0, rh_n=100, rh_max=52.0, rh_probes=4),
              _rack("r2", "b", f_sum=2300.0, f_n=100, f_max=24.0, f_in_band=100,
                    rh_sum=1900.0, rh_n=50, rh_max=40.0, rh_probes=2),
              _rack("r3", "c", f_sum=2300.0, f_n=100, f_max=24.0, f_in_band=100)])
    out = await estate.thermal(_FakeSession(), mode="live")
    rooms = {r["id"]: r for r in out["rooms"]}
    assert rooms["a"]["rh_avg"] == 45.0 and rooms["a"]["rh_max"] == 52.0
    assert rooms["c"]["rh_avg"] is None and rooms["c"]["rh_probes"] == 0
    assert rooms["c"]["compliance_pct"] == 100.0
    site = out["sites"][0]
    assert site["rh_avg"] == round(6400.0 / 150, 1)
    assert site["rh_max"] == 52.0 and site["rh_probes"] == 6
    assert out["totals"]["rh_probes"] == 6
    assert "6 rack PDU environment probes" in out["notes"][1]


@pytest.mark.asyncio
async def test_no_probe_anywhere_says_so_instead_of_inventing_humidity(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=2000.0, f_n=100, f_max=22.0, f_in_band=100)])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["totals"]["rh_avg"] is None
    assert "no rack humidity probe" in out["notes"][1]


@pytest.mark.asyncio
async def test_the_band_carries_both_ceilings(monkeypatch):
    """Warn and critical are two different lines - recommended and allowable -
    and the page must not have to invent the second one."""
    _thermal(monkeypatch, [], [])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["band"]["high_c"] == 27.0
    assert out["band"]["allowable_high_c"] == 32.0
    assert out["band"]["rh_high_pct"] == 60.0


@pytest.mark.asyncio
async def test_a_blank_delta_says_which_window_was_empty(monkeypatch):
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("b", "dc2", "DC2"), _room("c", "dc3", "DC3")],
             [_rack("r1", "a", f_sum=200.0, f_n=10, f_max=21.0, f_in_band=10),
              _rack("r2", "b", dc="dc2", code="DC2"),
              _rack("r3", "c", dc="dc3", code="DC3", f_sum=200.0, f_n=10, f_max=21.0,
                    f_in_band=10, c_sum=190.0, c_n=10, c_max=20.0)])
    out = await estate.thermal(_FakeSession(), mode="live")
    rooms = {r["id"]: r for r in out["rooms"]}
    assert rooms["a"]["delta_avg"] is None
    assert rooms["a"]["delta_note"] == "no readings in the previous hour"
    assert rooms["b"]["delta_note"] == "no readings in the last hour"
    assert rooms["c"]["delta_note"] is None and rooms["c"]["delta_avg"] == 1.0
    sites = {s["site_code"]: s for s in out["sites"]}
    assert sites["DC1"]["delta_note"] == "no readings in the previous hour"

    out = await estate.thermal(_FakeSession(), mode="daily",
                               focus=date(2026, 9, 6), compare=date(2026, 9, 5))
    assert out["rooms"][0]["delta_note"] == "no readings on 2026-09-05"
