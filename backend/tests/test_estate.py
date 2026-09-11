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


@pytest.fixture(autouse=True)
def _no_thermal_alarms(monkeypatch):
    """Open-condition counts are their own query against the alarm table.

    A test about temperature should not have to stub one, so the default is
    an estate with nothing open; the tests that are about the counts say so.
    """
    monkeypatch.setattr(estate.repo, "thermal_alarms", _returns(
        {"racks": {}, "rooms": {}, "sites": {}, "total": 0}))


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
          f_below=0, f_hot=0,
          p_sum=None, p_n=0, p_max=None, p_in_band=0, p_sensors=0,
          p_below=0, p_hot=0,
          pc_sum=None, pc_n=0, pc_max=None,
          n_sum=None, n_n=0, n_max=None, n_in_band=0, n_sensors=0,
          n_below=0, n_hot=0, nc_sum=None, nc_n=0, nc_max=None,
          e_sum=None, e_n=0, c_sum=None, c_n=0, c_max=None,
          rh_sum=None, rh_n=0, rh_max=None, rh_probes=0):
    return {
        "rack_id": rack_id, "rack_name": f"R-{rack_id}", "row_name": "A",
        "u_height": 42, "room_id": room_id, "room_name": f"room-{room_id}",
        "floor": "1", "room_class": "white_space", "datacenter_id": dc,
        "site_code": code, "site_name": code,
        "f_sum": f_sum, "f_n": f_n, "f_max": f_max, "f_in_band": f_in_band,
        "f_sensors": f_sensors, "f_below": f_below, "f_hot": f_hot,
        "p_sum": p_sum, "p_n": p_n, "p_max": p_max, "p_in_band": p_in_band,
        "p_sensors": p_sensors, "p_below": p_below, "p_hot": p_hot,
        "pc_sum": pc_sum, "pc_n": pc_n, "pc_max": pc_max,
        "n_sum": n_sum, "n_n": n_n, "n_max": n_max, "n_in_band": n_in_band,
        "n_sensors": n_sensors, "n_below": n_below, "n_hot": n_hot,
        "nc_sum": nc_sum, "nc_n": nc_n, "nc_max": nc_max,
        "e_sum": e_sum, "e_n": e_n, "c_sum": c_sum, "c_n": c_n, "c_max": c_max,
        "rh_sum": rh_sum, "rh_n": rh_n, "rh_max": rh_max, "rh_probes": rh_probes,
    }


def _thermal(monkeypatch, rooms, racks, p90=None, crahs=None):
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns(rooms))
    monkeypatch.setattr(estate.repo, "thermal_racks", _returns(racks))
    monkeypatch.setattr(estate.repo, "thermal_p90", _returns(
        p90 or {"racks": {}, "rooms": {}, "sites": {}, "total": None}))
    # The floor plant, which belongs to no rack. Empty unless a test says
    # otherwise, so a room with no cooling units reads as one - which is what
    # a plant room or an unimported hall actually is.
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns(crahs or {}))


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
    assert room["sources"] == {"probes": 1, "servers": 1, "network": 0}
    assert room["samples"] == 20
    assert "1 rack), else the servers' BMC inlet (1 rack)" in out["notes"][0]


@pytest.mark.asyncio
async def test_a_rack_of_switches_reports_its_front_panel_sensors(monkeypatch):
    """A spine or management rack holds no servers and often no probe.

    It used to read as a dash - no temperature, invisible to every column
    including the in-band share - while the switches in it published a
    front-panel reading the whole time. Third choice, never above a real
    intake sensor, and the row says which so nobody mistakes it for one.
    """
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("spine", "a", n_sum=54.0, n_n=2, n_max=27.5, n_in_band=2,
              n_sensors=2, nc_sum=52.0, nc_n=2, nc_max=26.5),
        _rack("compute", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
              f_sensors=18),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    spine = racks["spine"]
    assert spine["source"] == "network" and spine["sensors"] == 2
    assert spine["avg_c"] == 27.0 and spine["max_c"] == 27.5
    # It compares against itself, not against the compute rack's servers.
    assert spine["delta_avg"] == 1.0 and spine["delta_max"] == 1.0
    # And it counts: a silent rack contributed no in-band share at all.
    assert spine["compliance_pct"] == 100.0
    assert out["rooms"][0]["sources"] == {"probes": 0, "servers": 1, "network": 1}


@pytest.mark.asyncio
async def test_a_real_intake_sensor_always_wins(monkeypatch):
    """The front panel is a fallback, not a source. A rack with a probe or
    with servers must never be graded on a sensor behind a bezel."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("probe", "a", p_sum=46.0, p_n=2, p_max=23.5, p_in_band=2, p_sensors=1,
              n_sum=60.0, n_n=2, n_max=31.0, n_in_band=0, n_sensors=2),
        _rack("server", "a", f_sum=50.0, f_n=2, f_max=25.5, f_in_band=2, f_sensors=2,
              n_sum=60.0, n_n=2, n_max=31.0, n_in_band=0, n_sensors=2),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    assert racks["probe"]["source"] == "probes" and racks["probe"]["avg_c"] == 23.0
    assert racks["server"]["source"] == "servers" and racks["server"]["avg_c"] == 25.0


@pytest.mark.asyncio
async def test_the_note_names_the_third_source(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("spine", "a", n_sum=54.0, n_n=2, n_max=27.5, n_in_band=2, n_sensors=2),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert "front-panel sensor" in out["notes"][0]
    assert "1 rack)" in out["notes"][0]


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


@pytest.mark.asyncio
async def test_the_spread_partitions_every_reading_and_folds_by_count(monkeypatch):
    """Below / in band / above recommended / above allowable sum to 100 at
    every tier, and a room's split is its racks' readings pooled.

    Rack 1: 100 readings, 40 below, 50 in band, 10 above allowable.
    Rack 2: 300 readings, all in band. Room = 400: 10 % below, 87.5 % in,
    0 % above recommended, 2.5 % at risk - not the mean of the two racks'
    percentages (20 / 75 / 0 / 5).
    """
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("r1", "a", f_sum=2000.0, f_n=100, f_max=33.0, f_in_band=50,
              f_below=40, f_hot=10),
        _rack("r2", "a", f_sum=6600.0, f_n=300, f_max=23.0, f_in_band=300),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    assert racks["r1"]["below_pct"] == 40.0
    assert racks["r1"]["distribution"] == {
        "below_pct": 40.0, "in_band_pct": 50.0,
        "above_recommended_pct": 0.0, "above_allowable_pct": 10.0}
    room = out["rooms"][0]
    assert room["below_pct"] == 10.0
    assert room["distribution"] == {
        "below_pct": 10.0, "in_band_pct": 87.5,
        "above_recommended_pct": 0.0, "above_allowable_pct": 2.5}
    assert sum(room["distribution"].values()) == 100.0
    assert out["totals"]["distribution"] == room["distribution"]
    assert out["sites"][0]["below_pct"] == 10.0


@pytest.mark.asyncio
async def test_above_recommended_is_the_remainder(monkeypatch):
    """The warm-but-allowable share is what is left once the other three are
    counted, so the four always partition the readings."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("r1", "a", f_sum=2900.0, f_n=100, f_max=30.0, f_in_band=70,
              f_below=0, f_hot=0),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["racks"][0]["distribution"]["above_recommended_pct"] == 30.0


@pytest.mark.asyncio
async def test_the_spread_follows_the_intake_source(monkeypatch):
    """A rack with a probe takes the probe's split, not the servers'."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("r1", "a",
              f_sum=2500.0, f_n=100, f_max=27.0, f_in_band=100, f_below=0,
              p_sum=1700.0, p_n=100, p_max=19.0, p_in_band=20, p_below=80,
              p_sensors=1),
    ])
    out = await estate.thermal(_FakeSession(), mode="live")
    r = out["racks"][0]
    assert r["source"] == "probes"
    assert r["below_pct"] == 80.0


@pytest.mark.asyncio
async def test_p90_rides_on_the_tier_it_was_taken_over(monkeypatch):
    """p90 comes from one pooled query per tier and is attached by id; a
    row with no readings shows none even if the map has a stale entry."""
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("b", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=2000.0, f_n=100, f_max=24.0, f_in_band=100),
              _rack("r2", "b")],
             p90={"racks": {"r1": 23.44, "r2": 99.0},
                  "rooms": {"a": 23.44}, "sites": {"dc1": 23.44},
                  "total": 23.44})
    out = await estate.thermal(_FakeSession(), mode="live")
    racks = {r["id"]: r for r in out["racks"]}
    assert racks["r1"]["p90_c"] == 23.4
    assert racks["r2"]["p90_c"] is None
    rooms = {r["id"]: r for r in out["rooms"]}
    assert rooms["a"]["p90_c"] == 23.4
    assert rooms["b"]["p90_c"] is None
    assert out["sites"][0]["p90_c"] == 23.4
    assert out["totals"]["p90_c"] == 23.4


@pytest.mark.asyncio
async def test_a_silent_row_has_no_spread_and_no_p90(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_rack("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live")
    r = out["racks"][0]
    assert r["below_pct"] is None and r["distribution"] is None
    assert r["p90_c"] is None
    assert out["totals"]["p90_c"] is None
    assert out["totals"]["distribution"] is None


# ---------------------------------------------------------- thermal trend
# The trend is the table's readings over time. The repository is mocked; what
# is tested is the window, the bucket grid and the fill.


def _trend_rows(*rows):
    async def _fn(session, **kw):
        _fn.calls.append(kw)
        return list(rows)
    _fn.calls = []
    return _fn


@pytest.mark.asyncio
async def test_hourly_trend_draws_every_hour_in_the_window(monkeypatch):
    """A day of hourly points is 24 points, oldest first, ending at the top
    of the NEXT hour so the bucket in progress is on the chart; hours nothing
    reported in carry nulls rather than being skipped."""
    from datetime import UTC, datetime, timedelta
    fake = _trend_rows()
    monkeypatch.setattr(estate.repo, "thermal_trend", fake)
    out = await estate.thermal_trend(_FakeSession(), days=1, bucket="hour")
    assert len(out["points"]) == 24
    assert out["bucket"] == "hour" and out["days"] == 1
    now = datetime.now(UTC)
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    assert out["until"] == top
    assert out["since"] == top - timedelta(days=1)
    assert out["points"][0]["t"] == out["since"]
    assert out["points"][-1]["t"] == top - timedelta(hours=1)
    assert all(p["avg_c"] is None and p["sensors"] == 0 for p in out["points"])
    assert out["buckets_with_data"] == 0 and out["sensors"] == 0
    kw = fake.calls[0]
    assert kw["start"] == out["since"] and kw["end"] == out["until"]
    assert kw["bucket"] == "hour"
    # A day of hourly points is fine enough to read the five-minute rollup.
    assert kw["source"] == "5m" and out["source"] == "5m"


@pytest.mark.asyncio
async def test_a_week_of_hourly_points_reads_the_hourly_rollup(monkeypatch):
    """Beyond two days the five-minute table is too big a cold read; the
    hourly rollup gives the same hourly points from one value per sensor
    per hour. Its newest buckets close late, so the last three hours are
    re-read from the five-minute rollup and fill only what the hourly one
    lacked."""
    from datetime import timedelta
    fake = _trend_rows()
    monkeypatch.setattr(estate.repo, "thermal_trend", fake)
    out = await estate.thermal_trend(_FakeSession(), days=7, bucket="hour")
    assert len(out["points"]) == 168
    assert fake.calls[0]["source"] == "1h" and out["source"] == "1h"
    tail = fake.calls[1]
    assert tail["source"] == "5m"
    assert tail["end"] == out["until"] and tail["start"] == out["until"] - timedelta(hours=3)
    # And the newest two hours come from the hypertable, which no refresh
    # policy stands in front of.
    fresh = fake.calls[2]
    assert fresh["source"] == "raw"
    assert fresh["end"] == out["until"] and fresh["start"] == out["until"] - timedelta(hours=2)
    # Daily points take neither: a day bucket is read late anyway, and a day
    # of raw is a scan nobody needs for a month-long line.
    out = await estate.thermal_trend(_FakeSession(), days=30, bucket="day")
    assert fake.calls[3]["source"] == "1h" and len(fake.calls) == 4


@pytest.mark.asyncio
async def test_the_five_minute_tail_fills_gaps_and_the_raw_one_replaces(monkeypatch):
    """Two different jobs on the same tail.

    An hour the hourly rollup has not reached yet is MISSING, so the
    five-minute rollup fills it. The newest hour is not missing, it is a few
    minutes short - and a partial bucket drawn as a finished one is what made
    a fault look like it had not arrived - so the raw table replaces it.
    """
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    old = top - timedelta(hours=5)          # outside the two-hour raw tail
    gap = top - timedelta(hours=2)          # the hourly rollup never wrote it
    live = top - timedelta(hours=1)         # the hour in progress

    async def fake(session, **kw):
        if kw["source"] == "1h":
            return [{"b": old, "avg_c": 23.0, "p90_c": 23.3, "max_c": 24.0, "sensors": 80},
                    {"b": live, "avg_c": 23.1, "p90_c": 23.4, "max_c": 24.2, "sensors": 80}]
        if kw["source"] == "5m":
            return [{"b": gap, "avg_c": 25.0, "p90_c": 25.5, "max_c": 26.0, "sensors": 80},
                    {"b": live, "avg_c": 30.0, "p90_c": 30.0, "max_c": 30.0, "sensors": 80}]
        return [{"b": live, "avg_c": 35.0, "p90_c": 36.0, "max_c": 37.0, "sensors": 80}]
    monkeypatch.setattr(estate.repo, "thermal_trend", fake)
    out = await estate.thermal_trend(_FakeSession(), days=7, bucket="hour")
    by = {p["t"]: p for p in out["points"]}
    # Outside every tail, the hourly rollup stands.
    assert by[old]["avg_c"] == 23.0
    # A gap the hourly rollup left is filled, not overwritten by raw.
    assert by[gap]["avg_c"] == 25.0
    # The hour in progress is the hypertable's, not either rollup's.
    assert by[live]["avg_c"] == 35.0 and by[live]["max_c"] == 37.0
    assert out["buckets_with_data"] == 3


@pytest.mark.asyncio
async def test_trend_rows_land_on_their_bucket_and_are_rounded(monkeypatch):
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    top = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    b = top - timedelta(hours=3)
    monkeypatch.setattr(estate.repo, "thermal_trend", _trend_rows(
        {"b": b, "avg_c": 23.04, "p90_c": 23.66, "max_c": 24.16, "sensors": 40}))
    out = await estate.thermal_trend(_FakeSession(), days=1, bucket="hour")
    hit = [p for p in out["points"] if p["avg_c"] is not None]
    assert len(hit) == 1
    assert hit[0]["t"] == b
    assert (hit[0]["avg_c"], hit[0]["p90_c"], hit[0]["max_c"]) == (23.0, 23.7, 24.2)
    assert hit[0]["sensors"] == 40
    assert out["sensors"] == 40 and out["buckets_with_data"] == 1
    assert out["band"] == {"low_c": 18.0, "high_c": 27.0, "allowable_high_c": 32.0}


@pytest.mark.asyncio
async def test_a_picked_window_is_honoured_to_the_day(monkeypatch):
    from datetime import UTC, date, datetime, timedelta
    fake = _trend_rows()
    monkeypatch.setattr(estate.repo, "thermal_trend", fake)
    out = await estate.thermal_trend(_FakeSession(), bucket="day",
                                     since=date(2026, 9, 1), until=date(2026, 9, 10))
    assert out["days"] == 10
    assert len(out["points"]) == 10
    assert out["since"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert out["until"] == datetime(2026, 9, 11, tzinfo=UTC)
    assert out["points"][-1]["t"] == datetime(2026, 9, 10, tzinfo=UTC)
    assert out["points"][1]["t"] - out["points"][0]["t"] == timedelta(days=1)


@pytest.mark.asyncio
async def test_trend_window_refusals(monkeypatch):
    from datetime import date
    monkeypatch.setattr(estate.repo, "thermal_trend", _trend_rows())
    with pytest.raises(ValueError):
        await estate.thermal_trend(_FakeSession(), since=date(2026, 9, 2))
    with pytest.raises(ValueError):
        await estate.thermal_trend(_FakeSession(), since=date(2026, 9, 2),
                                   until=date(2026, 9, 1))
    with pytest.raises(ValueError):
        await estate.thermal_trend(_FakeSession(), since=date(2025, 1, 1),
                                   until=date(2026, 9, 1))
    with pytest.raises(ValueError):
        await estate.thermal_trend(_FakeSession(), days=0)
    with pytest.raises(ValueError):
        await estate.thermal_trend(_FakeSession(), bucket="week")


@pytest.mark.asyncio
async def test_trend_scope_is_passed_through(monkeypatch):
    fake = _trend_rows()
    monkeypatch.setattr(estate.repo, "thermal_trend", fake)
    out = await estate.thermal_trend(_FakeSession(), days=2, bucket="day",
                                     room_id="r", datacenter_id="s", rack_id="k")
    kw = fake.calls[0]
    assert (kw["room_id"], kw["datacenter_id"], kw["rack_id"]) == ("r", "s", "k")
    assert (out["room_id"], out["datacenter_id"], out["rack_id"]) == ("r", "s", "k")


# ------------------------------------------------------------- now mode
# An instant, not a window: one reading per SENSOR, nothing older than the
# horizon, and no comparison.


def _rack_now(rack_id, room_id, **kw):
    """A rack row as thermal_racks_now returns it.

    The compare columns are the SAME sensors read as of fifteen minutes ago,
    which is what the rate is measured against. Left empty unless a test sets
    them, which is the case of a rack that was reporting nothing back then.
    """
    row = _rack(rack_id, room_id, **kw)
    row.setdefault("pc_sum", None)
    row.setdefault("pc_n", 0)
    row.setdefault("pc_max", None)
    row.setdefault("c_sum", None)
    row.setdefault("c_n", 0)
    row.setdefault("c_max", None)
    return row


@pytest.mark.asyncio
async def test_now_reads_the_newest_reading_from_each_sensor(monkeypatch):
    """Two probes reporting 30.0 and 34.0 right now is an average of 32.0 over
    two readings, not two hundred."""
    seen = {}

    async def racks_now(session, **kw):
        seen.update(kw)
        return [_rack_now("r1", "a", p_sum=64.0, p_n=2, p_max=34.0,
                          p_in_band=0, p_hot=2, p_sensors=2)]

    async def p90_now(session, **kw):
        seen["p90_since"] = kw["since"]
        return {"racks": {"r1": 33.6}, "rooms": {"a": 33.6},
                "sites": {"dc1": 33.6}, "total": 33.6}

    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks_now", racks_now)
    monkeypatch.setattr(estate.repo, "thermal_p90_now", p90_now)
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    out = await estate.thermal(_FakeSession(), mode="now")

    rack = out["racks"][0]
    assert rack["avg_c"] == 32.0 and rack["max_c"] == 34.0 and rack["samples"] == 2
    assert rack["p90_c"] == 33.6
    assert rack["compliance_pct"] == 0.0
    assert rack["distribution"]["above_allowable_pct"] == 100.0
    assert out["window"]["mode"] == "now" and out["window"]["label"] == "now"
    # The horizon is applied, and both queries read the same instant.
    assert seen["since"] == out["window"]["focus_start"]
    assert seen["p90_since"] == seen["since"]
    assert out["window"]["focus_end"] - seen["since"] == estate.repo.NOW_HORIZON


@pytest.mark.asyncio
async def test_a_rack_that_was_not_reporting_earlier_has_no_rate(monkeypatch):
    """A probe fitted this morning has nothing behind it, and a rate against
    nothing would read as a plunge on a rack that has simply just arrived."""
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks_now", _returns(
        [_rack_now("r1", "a", p_sum=46.0, p_n=2, p_max=23.5, p_in_band=2, p_sensors=2)]))
    monkeypatch.setattr(estate.repo, "thermal_p90_now", _returns(
        {"racks": {}, "rooms": {}, "sites": {}, "total": None}))
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    out = await estate.thermal(_FakeSession(), mode="now")

    rack = out["racks"][0]
    assert rack["delta_avg"] is None and rack["rate_k_per_h"] is None
    assert "fifteen minutes ago" in rack["delta_note"]
    assert any("newest reading from each sensor" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_now_reports_a_rate_per_hour(monkeypatch):
    """Two probes at 23.0 now against 21.0 fifteen minutes ago is 2 K in a
    quarter hour, which is 8 K/hour - the unit the ASHRAE limit is written in
    and the one an operator estimates time-to-trouble with."""
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks_now", _returns(
        [_rack_now("r1", "a", p_sum=46.0, p_n=2, p_max=23.0, p_in_band=2,
                   p_sensors=2, pc_sum=42.0, pc_n=2, pc_max=21.0)]))
    monkeypatch.setattr(estate.repo, "thermal_p90_now", _returns(
        {"racks": {}, "rooms": {}, "sites": {}, "total": None}))
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    out = await estate.thermal(_FakeSession(), mode="now")

    rack = out["racks"][0]
    assert rack["delta_avg"] == 2.0
    assert rack["rate_k_per_h"] == 8.0
    # And it folds: one rack is the room and the room is the site.
    assert out["rooms"][0]["rate_k_per_h"] == 8.0
    assert out["sites"][0]["rate_k_per_h"] == 8.0
    assert out["band"]["rate_limit_k_per_h"] == 20.0
    assert out["band"]["rate_window_minutes"] == 15


@pytest.mark.asyncio
async def test_a_falling_hall_reports_a_negative_rate(monkeypatch):
    """Direction is half the finding. A steep fall is proof a fix worked, and
    on a floor with tape it is a violation in its own right."""
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks_now", _returns(
        [_rack_now("r1", "a", p_sum=44.0, p_n=2, p_max=22.0, p_in_band=2,
                   p_sensors=2, pc_sum=50.0, pc_n=2, pc_max=25.0)]))
    monkeypatch.setattr(estate.repo, "thermal_p90_now", _returns(
        {"racks": {}, "rooms": {}, "sites": {}, "total": None}))
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    out = await estate.thermal(_FakeSession(), mode="now")
    assert out["racks"][0]["rate_k_per_h"] == -12.0


@pytest.mark.asyncio
async def test_the_other_modes_have_no_rate(monkeypatch):
    """A day against another day is a real change and a fictional rate: the
    hours between them carry different weather and different load, and
    spreading the difference over 24 of them describes none of it."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [
        _rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
              f_sensors=18, c_sum=440.0, c_n=18, c_max=25.0),
    ])
    out = await estate.thermal(_FakeSession(), mode="daily")
    assert out["racks"][0]["delta_avg"] is not None
    assert out["racks"][0]["rate_k_per_h"] is None
    assert out["band"]["rate_window_minutes"] is None


@pytest.mark.asyncio
async def test_a_silent_rack_is_absent_in_now_too(monkeypatch):
    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks_now", _returns([_rack_now("r1", "a")]))
    monkeypatch.setattr(estate.repo, "thermal_p90_now", _returns(
        {"racks": {}, "rooms": {}, "sites": {}, "total": None}))
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    out = await estate.thermal(_FakeSession(), mode="now")
    rack = out["racks"][0]
    assert rack["avg_c"] is None and rack["source"] is None
    assert "ten minutes" in rack["note"]


@pytest.mark.asyncio
async def test_the_alarm_count_lands_on_the_row_that_raised_it(monkeypatch):
    """A condition is placed where its DEVICE is, and every tier adds up.

    The count and the drill-down take the same category list, so a number an
    operator clicks cannot disagree with the rows behind it.
    """
    monkeypatch.setattr(estate.repo, "thermal_alarms", _returns(
        {"racks": {"r1": 3}, "rooms": {"a": 5}, "sites": {"dc1": 5}, "total": 5}))
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=2000.0, f_n=100, f_max=24.0, f_in_band=100),
              _rack("r2", "a")])
    out = await estate.thermal(_FakeSession(), mode="live")

    racks = {r["id"]: r for r in out["racks"]}
    assert racks["r1"]["alarms_open"] == 3
    # Nothing open is zero, not absent: a dash would read as "not counted".
    assert racks["r2"]["alarms_open"] == 0
    # The room carries what its racks raised AND what nothing in a rack did.
    assert out["rooms"][0]["alarms_open"] == 5
    assert out["sites"][0]["alarms_open"] == 5
    assert out["totals"]["alarms_open"] == 5
    assert out["alarm_categories"] == ["cooling", "environmental"]
    # Where the difference went. A CRAH stands on the floor and belongs to no
    # rack, so a hall shows five while its racks account for three - correct,
    # and unreadable unless the row says which is which.
    assert out["rooms"][0]["alarms_in_racks"] == 3
    assert out["sites"][0]["alarms_in_racks"] == 3
    assert out["totals"]["alarms_in_racks"] == 3
    # A rack has nothing beneath it, so all of its own are in it.
    assert racks["r1"]["alarms_in_racks"] == 3


@pytest.mark.asyncio
async def test_an_intake_alarm_on_a_server_is_not_a_thermal_page_count(monkeypatch):
    """The category records the FAILING THING. A hot server is a host, and it
    is counted beside the rest of its kit on the home page; this page counts
    the air and the plant that moves it."""
    seen = {}

    async def counts(session, **kw):
        seen.update(kw)
        return {"racks": {}, "rooms": {}, "sites": {}, "total": 0}

    monkeypatch.setattr(estate.repo, "thermal_alarms", counts)
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_rack("r1", "a")])
    await estate.thermal(_FakeSession(), mode="live")
    assert seen["categories"] == ["cooling", "environmental"]
    assert "it_equipment" not in seen["categories"]



# ── the floor plant, beside the racks it serves ──────────────────────────────
#
# Everything else on this page is racks added up. Cooling units belong to no
# rack, so until now a hall's supply-versus-return story - the page's own
# thesis, and the difference between sending an engineer to the machine or to
# the plant room - only appeared after drilling into the room.


def _crah(device_id, name, supply, ret, setpoint=22.0, running=True):
    return {"device_id": device_id, "name": name, "supply_c": supply,
            "return_c": ret, "setpoint_c": setpoint, "running": running}


@pytest.mark.asyncio
async def test_a_room_row_carries_its_cooling_units(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)],
             crahs={"a": [_crah("c1", "CRAH1", 22.0, 30.0),
                          _crah("c2", "CRAH2", 22.0, 30.0)]})
    out = await estate.thermal(_FakeSession(), mode="live")
    cooling = out["rooms"][0]["cooling"]
    assert cooling["units"] == 2
    assert cooling["supply_c"] == 22.0 and cooling["return_c"] == 30.0
    assert cooling["delta_t_k"] == 8.0


@pytest.mark.asyncio
async def test_a_stopped_unit_is_counted_and_left_out_of_the_temperatures(monkeypatch):
    """It is still one of the room's units and it is not cooling anything. Its
    discharge has soaked up to its return, so averaging it in would report a
    hall with no air-side delta as a hall with a small one."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)],
             crahs={"a": [_crah("c1", "CRAH1", 22.0, 30.0),
                          _crah("c2", "CRAH2", 29.8, 30.0, running=False)]})
    out = await estate.thermal(_FakeSession(), mode="live")
    cooling = out["rooms"][0]["cooling"]
    assert cooling["units"] == 2 and cooling["units_stopped"] == 1
    assert cooling["delta_t_k"] == 8.0


@pytest.mark.asyncio
async def test_a_room_with_no_cooling_units_says_nothing(monkeypatch):
    """A plant room, or a hall whose units are not imported yet. No units and
    no units misbehaving are different statements, and a zero would be the
    second one."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["rooms"][0]["cooling"] is None


@pytest.mark.asyncio
async def test_a_site_adds_the_counts_and_keeps_the_temperatures_local(monkeypatch):
    """A mean of two halls' supply air describes neither of them, and a delta
    folded across rooms is a number about nowhere."""
    _thermal(monkeypatch,
             [_room("a", "dc1", "DC1"), _room("b", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18),
              _rack("r2", "b", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)],
             crahs={"a": [_crah("c1", "CRAH1", 18.0, 26.0)],
                    "b": [_crah("c2", "CRAH2", 24.0, 30.0, running=False)]})
    out = await estate.thermal(_FakeSession(), mode="live")
    site = out["sites"][0]["cooling"]
    assert site["units"] == 2 and site["units_stopped"] == 1
    assert site["supply_c"] is None and site["delta_t_k"] is None


@pytest.mark.asyncio
async def test_the_estate_row_and_the_room_view_agree_about_one_hall(monkeypatch):
    """The whole reason the counting lives in one function. A hall described
    one way on the table and another way one click deeper is worse than a hall
    described neither way."""
    from app.services import thermal as thermal_svc

    units = [_crah("c1", "CRAH1", 29.0, 31.0), _crah("c2", "CRAH2", 22.0, 38.0)]
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)],
             crahs={"a": units})
    out = await estate.thermal(_FakeSession(), mode="live")
    row = out["rooms"][0]["cooling"]

    modelled = [thermal_svc.CrahThermal(
        device_id=u["device_id"], name=u["name"], supply_c=u["supply_c"],
        return_c=u["return_c"], setpoint_c=u["setpoint_c"], running=u["running"])
        for u in units]
    view = thermal_svc.RoomThermal(
        room_id="a", crahs=modelled,
        return_p90=thermal_svc.percentile([u["return_c"] for u in units], 90),
    ).as_dict()

    assert row["units_high_supply"] == view["units_high_supply"]
    assert row["units_high_return"] == view["units_high_return"]
    assert row["delta_t_k"] == view["room_delta_t_k"]


# ── pinning the page to one source ──────────────────────────────────────────
#
# The automatic rule picks the sensor closest to the air a rack breathes, which
# is the right answer every day. It cannot answer the other question: does this
# hall read the same by probe as it does by BMC? A probe that has drifted shows
# up only as a hall disagreeing with itself, and that needs both readings.


def _both_sources(rack_id, room_id):
    """A rack with a probe AND servers, reading differently."""
    return _rack(rack_id, room_id,
                 p_sum=40.0, p_n=2, p_max=20.5, p_in_band=2, p_sensors=2,
                 f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18, f_sensors=18)


@pytest.mark.asyncio
async def test_auto_takes_the_probe_where_there_is_one(monkeypatch):
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_both_sources("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live")
    assert out["racks"][0]["source"] == "probes"
    assert out["racks"][0]["avg_c"] == 20.0
    assert out["source"] == "auto"


@pytest.mark.asyncio
async def test_pinning_to_servers_reads_the_same_rack_differently(monkeypatch):
    """The whole point: one hall, two sources, two answers - and the gap
    between them is the finding."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_both_sources("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live", source="servers")
    assert out["racks"][0]["source"] == "servers"
    assert out["racks"][0]["avg_c"] == 25.0
    assert out["racks"][0]["sensors"] == 18
    assert out["source"] == "servers"


@pytest.mark.asyncio
async def test_a_pinned_rack_with_no_such_sensor_is_silent(monkeypatch):
    """It must NOT fall through to another source. "These racks have no probe"
    is the answer somebody pinned the page to get."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")],
             [_rack("r1", "a", f_sum=450.0, f_n=18, f_max=26.0, f_in_band=18,
                    f_sensors=18)])
    out = await estate.thermal(_FakeSession(), mode="live", source="probes")
    rack = out["racks"][0]
    assert rack["source"] is None and rack["avg_c"] is None
    assert rack["samples"] == 0


@pytest.mark.asyncio
async def test_the_percentile_follows_the_pin(monkeypatch):
    """A percentile taken over a different population from the average beside
    it is two answers to one question."""
    seen = {}

    async def p90(session, **kw):
        seen.update(kw)
        return {"racks": {}, "rooms": {}, "sites": {}, "total": None}

    monkeypatch.setattr(estate.repo, "thermal_rooms", _returns([_room("a", "dc1", "DC1")]))
    monkeypatch.setattr(estate.repo, "thermal_racks", _returns([_both_sources("r1", "a")]))
    monkeypatch.setattr(estate.repo, "thermal_p90", p90)
    monkeypatch.setattr(estate.thermal_repo, "crahs_by_room", _returns({}))
    await estate.thermal(_FakeSession(), mode="live", source="probes")
    assert seen["force"] == "ambient_temperature"


@pytest.mark.asyncio
async def test_an_unknown_source_falls_back_to_the_rule(monkeypatch):
    """A stale bookmark or a typed URL must not blank the page."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_both_sources("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live", source="nonsense")
    assert out["source"] == "auto"
    assert out["racks"][0]["source"] == "probes"


@pytest.mark.asyncio
async def test_the_note_says_which_source_is_pinned(monkeypatch):
    """A page pinned to probes and a page choosing per rack are different
    measurements of the same estate, and a printout of either should say so."""
    _thermal(monkeypatch, [_room("a", "dc1", "DC1")], [_both_sources("r1", "a")])
    out = await estate.thermal(_FakeSession(), mode="live", source="probes")
    assert "Pinned to one source" in out["notes"][0]
    assert "front environment probe" in out["notes"][0]


@pytest.mark.asyncio
async def test_the_trend_follows_the_pin_the_table_is_using(monkeypatch):
    """One screen, one measurement.

    A reader who pins the table to PROBES and gets a chart still drawn the
    automatic way is looking at two readings of one estate with nothing
    saying they differ - which is the failure the pin exists to expose, not
    one it should introduce.
    """
    seen: list[str] = []

    async def _trend(_s, **kw):
        seen.append(kw.get("force", ""))
        return []

    monkeypatch.setattr(estate.repo, "thermal_trend", _trend)
    await estate.thermal_trend(_FakeSession(), days=7, bucket="hour",
                               source="probes")
    # Every read behind the one line: the rollup, its fine tail, the raw
    # newest bucket. A pin honoured by some of them would seam the line.
    assert seen and all(f == "ambient_temperature" for f in seen)

    seen.clear()
    await estate.thermal_trend(_FakeSession(), days=7, bucket="hour")
    assert seen and all(f == "" for f in seen)


@pytest.mark.asyncio
async def test_the_rollup_field_still_means_the_rollup(monkeypatch):
    """`source` on the trend payload named which ROLLUP the points came from
    long before there was an intake pin. The pin gets its own name rather
    than quietly taking that one over."""
    monkeypatch.setattr(estate.repo, "thermal_trend", _returns([]))
    out = await estate.thermal_trend(_FakeSession(), days=1, bucket="hour",
                                     source="servers")
    assert out["source"] in ("5m", "1h", "raw")
    assert out["intake_source"] == "servers"
