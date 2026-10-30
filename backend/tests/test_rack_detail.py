"""The rack page reads the Power and Thermal pages' rack rows for one rack.

What must hold: every rack-level read takes the rack filter (a 30-second
refresh of one page must not cost the estate), the blocks compose from the
same helpers the estate pages use, and the grading is the room's ASHRAE
class. The SQL is proved by running it; the shape is what is pinned here.
"""

from __future__ import annotations

import pytest

from app.repositories import estate as estate_repo
from app.services import racks as service

RACK = "11111111-1111-1111-1111-111111111111"


class _FakeSession:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql: list[str] = []
        self.params: list[dict] = []

    async def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(params or {})
        rows = self.rows

        class _Result:
            def mappings(self):
                return self

            def all(self):
                return rows

            def one(self):
                return rows[0] if rows else {}

            def first(self):
                return rows[0] if rows else None

        return _Result()


@pytest.mark.asyncio
@pytest.mark.parametrize("read", [
    lambda s: estate_repo.power_racks_live(s, rack_id=RACK),
    lambda s: estate_repo.power_inventory(s, rack_id=RACK),
    lambda s: estate_repo.power_p95(s, rack_id=RACK),
    lambda s: estate_repo.power_rack_legs(s, rack_id=RACK),
    lambda s: estate_repo.power_rack_psu_failed(s, rack_id=RACK),
    lambda s: estate_repo.power_rack_strip_state(s, rack_id=RACK),
    lambda s: estate_repo.power_rack_outlet_loads(s, rack_id=RACK),
])
async def test_every_rack_level_read_takes_the_rack_filter(read):
    session = _FakeSession()
    await read(session)
    scoped = [sql for sql in session.sql if "rack_id" in sql]
    assert scoped, "a rack-level read with no rack in it"
    for sql, params in zip(session.sql, session.params, strict=True):
        if "d.rack_id IS NOT NULL" in sql or "dt.category IN ('it', 'network')" in sql:
            assert "d.rack_id = CAST(:rack AS uuid)" in sql, sql[:200]
            assert params.get("rack") == RACK


@pytest.mark.asyncio
async def test_unscoped_reads_are_unchanged():
    """The estate pages still read every rack: no stray filter, no stray
    parameter."""
    session = _FakeSession()
    await estate_repo.power_rack_strip_state(session)
    await estate_repo.power_rack_legs(session)
    for sql, params in zip(session.sql, session.params, strict=True):
        assert "CAST(:rack AS uuid)" not in sql and "rack" not in params


def _dev(name, cat, u, h=1, **kw):
    base = {"id": name.lower(), "name": name, "device_type": "server", "category": cat,
            "u_start": u, "u_height": h, "status": "ONLINE", "max_severity": "CLEAR",
            "active_alarms": 0}
    base.update(kw)
    return base


def test_space_counts_units_and_names_the_largest_free_run():
    rack = {"u_height": 42}
    devices = [_dev("SRV01", "it", 1, 2), _dev("SRV02", "it", 3, 2), _dev("SW1", "network", 42),
               _dev("PDUA", "power", None, device_type="pdu")]
    sp = service.space_block(rack, devices)
    assert (sp["u_total"], sp["u_used"], sp["u_free"]) == (42, 5, 37)
    assert sp["largest_free"] == {"u_start": 5, "u_height": 37}
    assert sp["racked"] == 3 and sp["zero_u"] == 1
    assert sp["by_category"] == {"it": 2, "network": 1, "power": 1}


def test_alarms_roll_up_to_the_worst_open_one():
    devices = [_dev("A", "it", 1, active_alarms=2, max_severity="MINOR"),
               _dev("B", "it", 2, active_alarms=1, max_severity="MAJOR"),
               _dev("C", "it", 3, max_severity="CRITICAL"),      # nothing open: stale rollup
               _dev("D", "it", 4, status="OFFLINE")]
    a = service.alarm_block(devices)
    assert a == {"open": 3, "worst": "MAJOR", "devices": 2, "offline": 1}
    assert service.alarm_block([])["worst"] == "CLEAR"


def _pt(name, cat, dtype="server", **kw):
    base = {"id": name.lower(), "name": name, "device_type": dtype, "category": cat,
            "u_start": 1, "u_height": 1, "inlet_c": None, "exhaust_c": None,
            "ambient_c": None, "component_c": None, "cpu_c": None, "rh_pct": None,
            "read_at": None, "ashrae_class": "A1"}
    base.update(kw)
    return base


def test_thermal_pools_the_intakes_the_thermal_page_pools_and_grades_them():
    """A server's inlet, a switch's component temperature, a probe's ambient;
    the exhaust only where a device reports one. The hot-spot rule is the
    room index's."""
    t = service.thermal_block([
        _pt("SRV01", "it", inlet_c=22.0, exhaust_c=34.0),
        _pt("SRV02", "it", inlet_c=28.5, exhaust_c=40.0),
        _pt("SW1", "network", dtype="switch", component_c=25.0),
        _pt("SEN1", "environment", dtype="sensor", ambient_c=21.5, rh_pct=45.0),
        _pt("PDUA", "power", dtype="pdu", ambient_c=23.0),
        _pt("GW", "facility", dtype="modbus_gateway"),          # no air: dropped
    ])
    assert [p["kind"] for p in t["points"]] == ["device", "device", "device", "probe", "strip"]
    assert t["sensors"] == 5 and t["in_band"] == 4
    assert t["intake_max_c"] == 28.5 and t["intake_min_c"] == 21.5
    assert t["exhaust_mean_c"] == 37.0 and t["delta_t_k"] == 13.0
    assert t["hot"] == "allowable"
    assert t["ashrae"]["class"] == "A1" and t["ashrae"]["rec_high_c"] == 27.0
    assert t["points"][1]["grade"] != t["points"][0]["grade"]


def test_thermal_hot_spot_out_of_envelope_and_empty_rack():
    assert service.thermal_block([_pt("S", "it", inlet_c=33.0)])["hot"] == "out"
    empty = service.thermal_block([])
    assert empty["sensors"] == 0 and empty["hot"] is None and empty["points"] == []


@pytest.mark.asyncio
async def test_power_block_is_the_estate_row_plus_the_week(monkeypatch):
    """Headroom and stranded come from the same p95 the Power page uses; the
    strips carry their own comm state, since a dead strip is a dead feed."""
    live = [{"rack_id": RACK, "rack_name": "R1-01", "rated_power_kw": 8.0, "room_id": "rm",
             "room_name": "Hall", "floor": None, "room_class": "white_space",
             "datacenter_id": "dc", "site_code": "DC1", "site_name": "Site",
             "devices": 3, "avg_it": 3.2, "loads": 2, "reporting": 2, "at_strip": 0,
             "prev_it": 3.0, "as_of": None}]
    inv = {"racks": {RACK: {"allocated_kw": 6.0, "unrated": 0, "reserved_kw": None}},
           "feeds": {RACK: {"a": ["PDUA"], "b": ["PDUB"], "dual": 2, "single_cord": 0, "unfed": 0}},
           "room_design_kw": {}, "site_design_kw": {}}
    strips = {RACK: [
        {"strip_id": "pdua", "strip": "PDUA", "side": "A", "rated_kw": 7.4, "kw": 1.6,
         "legs": [7.0], "v_ln": 230.0, "bank_a": {}, "breaker_a": {}, "outlet_a": {}},
        {"strip_id": "pdub", "strip": "PDUB", "side": "B", "rated_kw": 7.4, "kw": 1.6,
         "legs": [7.0], "v_ln": 230.0, "bank_a": {}, "breaker_a": {}, "outlet_a": {}},
    ]}

    async def fake(value):
        async def f(session, **kw):
            assert kw.get("rack_id") == RACK
            return value
        return f

    monkeypatch.setattr(estate_repo, "power_racks_live", await fake(live))
    monkeypatch.setattr(estate_repo, "power_inventory", await fake(inv))
    from app.repositories import rack_detail

    async def fake_p95(session, rack_id, *, days=7):
        assert rack_id == RACK and days == 7
        return 4.5
    monkeypatch.setattr(rack_detail, "rack_p95", fake_p95)
    monkeypatch.setattr(estate_repo, "power_rack_legs",
                        await fake({RACK: [{"strip_id": "pdua", "strip": "PDUA",
                                            "legs": [7.0, 5.0, 6.0], "imbalance_pct": 16.7}]}))
    monkeypatch.setattr(estate_repo, "power_rack_psu_failed", await fake({RACK: ["SRV02 PSU2"]}))
    monkeypatch.setattr(estate_repo, "power_rack_strip_state", await fake(strips))

    async def no_upstream(session, racks):
        racks[0]["feeds"] = {**racks[0]["feeds"], "shared_upstream": []}
    from app.services import estate as estate_service
    monkeypatch.setattr(estate_service, "_attach_shared_upstream", no_upstream)

    devices = [_dev("PDUA", "power", None, id="pdua", device_type="pdu", status="ONLINE"),
               _dev("PDUB", "power", None, id="pdub", device_type="pdu", status="OFFLINE")]
    p = await service.power_block(object(), RACK, devices)
    assert p["it_ac_kw"] == 3.2 and p["rated_kw"] == 8.0 and p["load_pct"] == 40.0
    assert p["p95_kw"] == 4.5 and p["headroom_kw"] == 3.5 and p["stranded_kw"] == 1.5
    assert p["imbalance_pct"] == 16.7 and p["psu_failed"] == ["SRV02 PSU2"]
    assert [s["side"] for s in p["strips"]] == ["A", "B"]
    assert p["strips"][1]["status"] == "OFFLINE"
    assert p["failover"] is not None and p["feeds"]["shared_upstream"] == []
    assert p["thresholds"]["bank_warn_pct"] == 80.0
    assert not any(k.startswith("_") for k in p)


def test_device_rows_carry_numbers_not_decimals():
    """device_state's numeric columns arrive as Decimal; Pydantic would hand
    them to JSON as strings and the page's toFixed would throw."""
    from decimal import Decimal
    row = service._plain({"id": "x", "power_w": Decimal("200.00"), "inlet_temp_c": None,
                          "cpu_util_pct": Decimal("64.00"), "name": "SRV"})
    assert row["power_w"] == 200.0 and isinstance(row["power_w"], float)
    assert row["inlet_temp_c"] is None and row["cpu_util_pct"] == 64.0 and row["name"] == "SRV"


@pytest.mark.asyncio
async def test_scoped_telemetry_reads_name_the_racks_devices():
    """The week of hourly points and the ten minutes of raw samples are read
    for the rack's devices only; unscoped, they are read for the estate."""
    session = _FakeSession()
    await estate_repo.power_racks_live(session, rack_id=RACK)
    sql = session.sql[0]
    assert "WHERE m.key = 'power_draw' AND t.device_id IN (SELECT id FROM device" in sql
    assert "WHERE m.key = 'outlet_active_power' AND t.device_id IN" in sql
    session = _FakeSession()
    await estate_repo.power_racks_live(session)
    assert "t.device_id IN (SELECT id FROM device" not in session.sql[0]
    # The week's p95 has a read of its own for one rack, same arithmetic.
    from app.repositories import rack_detail
    session = _FakeSession(rows=[{"p95_kw": 4.456}])
    assert await rack_detail.rack_p95(session, RACK) == 4.46
    assert "percentile_cont(0.95)" in session.sql[0] and session.params[0]["days"] == 7
