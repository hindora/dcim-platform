"""The plant chain: what gets pooled, what gets ranged, and what gets judged.

The expensive mistakes on a plant page are not arithmetic either. They are:
calling a staged-off chiller a fault, calling a tripped one standby, averaging
two sites' headers into a temperature neither of them has, and reporting N+1
on capacity that is sitting cold and would take twenty minutes to start.
"""

from __future__ import annotations

import pytest

from app.services import plant


class _FakeSession:
    """The service never touches it; the repository calls are patched out."""


def _returns(value):
    async def _f(*_a, **_k):
        return value
    return _f


def _machine(device_id: str, name: str, dtype: str, *, site="s1", code="DC1",
             room="Central Plant", cooling_w=None, power_w=None,
             room_class=None, room_type="plant", room_id="r1"):
    return {
        "device_id": device_id, "name": name, "device_type": dtype,
        "status": "ONLINE", "room_id": room_id, "room_name": room,
        "room_type": room_type, "room_class": room_class, "floor": "1",
        "site_id": site, "site_code": code, "site_name": code,
        "model_name": f"model-{dtype}", "rated_cooling_w": cooling_w,
        "rated_power_w": power_w,
        # Monitored by default: the interesting cases are the exceptions.
        "endpoints": 1,
    }


def _gear(device_id: str, name: str, dtype: str, *, room="UPS Room",
          room_id="r2", room_type="electrical"):
    """A machine in a FACILITY room, which is what grows a room row."""
    return _machine(device_id, name, dtype, room=room, room_id=room_id,
                    room_type=room_type, room_class="facility")


def _room(out, name: str) -> dict:
    return next(r for r in out["facility_rooms"] if r["name"] == name)


def _patch(monkeypatch, machines, values=None, flags=None, alarms=None,
           observed=None):
    monkeypatch.setattr(plant.repo, "machines", _returns(machines))
    monkeypatch.setattr(plant.repo, "latest", _returns(values or {}))
    monkeypatch.setattr(plant.repo, "flags", _returns(flags or {}))
    monkeypatch.setattr(plant.repo, "alarms", _returns(alarms or {}))
    monkeypatch.setattr(plant.repo, "observed_kw", _returns(observed or {}))


def _chiller_values(supply: float, ret: float, flow: float, *, power_w=100_000.0,
                    cop=5.0) -> dict:
    return {
        ("water_supply_temp", "CHW"): supply,
        ("water_return_temp", "CHW"): ret,
        ("water_flow", "CHW"): flow,
        ("power_draw", ""): power_w,
        ("cop", ""): cop,
    }


def _stage(out, code: str, key: str) -> dict:
    return next(s for s in out["stages"]
                if s["site_code"] == code and s["stage"] == key)


# --- the physical boundary ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_stage_never_pools_two_sites(monkeypatch):
    """DC1's chilled-water header is not DC2's.

    Pooling them would report a supply temperature no header in the estate is
    actually running at, and hide a site whose water is warm behind one whose
    water is cold.
    """
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", site="s1", code="DC1",
                     cooling_w=800_000),
            _machine("c2", "CH2", "chiller", site="s2", code="DC2",
                     cooling_w=800_000)],
           values={"c1": _chiller_values(7.0, 13.0, 20.0),
                   "c2": _chiller_values(11.0, 13.0, 20.0)},
           flags={"c1": {"running": True, "alarm_points": []},
                  "c2": {"running": True, "alarm_points": []}})

    out = await plant.plant(_FakeSession())
    assert _stage(out, "DC1", "chiller")["delta_t_k"] == 6.0
    assert _stage(out, "DC2", "chiller")["delta_t_k"] == 2.0
    assert {s["site_code"] for s in out["stages"]} == {"DC1", "DC2"}


@pytest.mark.asyncio
async def test_independent_loops_are_ranged_never_averaged(monkeypatch):
    """Twelve CDUs are twelve loops. Their mean ΔT describes no loop at all."""
    _patch(monkeypatch,
           [_machine("d1", "CDU1", "cdu", room="Server Hall A", cooling_w=80_000),
            _machine("d2", "CDU2", "cdu", room="Server Hall B", cooling_w=80_000)],
           values={"d1": {("water_supply_temp", "TCS"): 32.0,
                          ("water_return_temp", "TCS"): 45.0,
                          ("thermal_load", ""): 40_000.0},
                   "d2": {("water_supply_temp", "TCS"): 32.0,
                          ("water_return_temp", "TCS"): 38.0,
                          ("thermal_load", ""): 20_000.0}},
           flags={"d1": {"running": True, "alarm_points": []},
                  "d2": {"running": True, "alarm_points": []}})

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "cdu")
    assert stage["shared_header"] is False
    assert stage["delta_t_k"] is None
    assert (stage["delta_t_min_k"], stage["delta_t_max_k"]) == (6.0, 13.0)
    # Counts still fold: heat is additive however many loops carry it.
    assert stage["heat_kw"] == 60.0


# --- staged off is not the same thing everywhere ------------------------------

@pytest.mark.asyncio
async def test_a_quiet_chiller_is_standby_and_a_quiet_crah_is_not(monkeypatch):
    """A chiller plant is designed to run fewer machines than it owns.

    A hall's CRAHs normally all turn, so one that has dropped out is a unit
    that needs looking at - and the word has to match the thermal page's.
    """
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000),
            _machine("a1", "CRAH1", "crah", room="Server Hall A",
                     cooling_w=100_000)],
           flags={"c1": {"running": False, "alarm_points": []},
                  "a1": {"running": False, "alarm_points": []}})

    by_name = {m["name"]: m for m in (await plant.plant(_FakeSession()))["machines"]}
    assert by_name["CH1"]["verdict"] == "standby"
    assert by_name["CRAH1"]["verdict"] == "stopped"


@pytest.mark.asyncio
async def test_stopped_with_a_condition_open_is_tripped(monkeypatch):
    """Staged off and tripped both draw nothing. Only the alarm separates them."""
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000)],
           flags={"c1": {"running": False, "alarm_points": []}},
           alarms={"c1": {"open": 2, "worst": 3}})

    machine = (await plant.plant(_FakeSession()))["machines"][0]
    assert machine["verdict"] == "tripped"
    assert "2 open condition" in machine["why"]


@pytest.mark.asyncio
async def test_a_fault_binary_with_nothing_raised_is_still_reported(monkeypatch):
    """The machine says Filter_Dirty and the platform holds no condition.

    Either no rule covers the point or a clear was missed. Both are worth a
    look, and neither is a healthy machine.
    """
    _patch(monkeypatch,
           [_machine("a1", "CRAH1", "crah", room="Server Hall A",
                     cooling_w=100_000)],
           values={"a1": {("supply_air_temp", ""): 22.0,
                          ("return_air_temp", ""): 27.0,
                          ("air_setpoint_temp", ""): 22.0}},
           flags={"a1": {"running": True, "alarm_points": ["Filter_Dirty"]}})

    machine = (await plant.plant(_FakeSession()))["machines"][0]
    assert machine["verdict"] == "fault_signal"
    assert "Filter_Dirty" in machine["why"]


# --- redundancy is judged on what is already turning --------------------------

@pytest.mark.asyncio
async def test_n_plus_1_is_about_the_running_set(monkeypatch):
    """A standby machine does not help in the minutes after a trip.

    Two 800 kW chillers running against a small load survive losing one; the
    same load on one machine does not, however many are sitting cold.
    """
    # 10 L/s across 5 K is 209 kW a machine: 419 kW carried by two, which one
    # 800 kW machine could still hold on its own.
    values = {"c1": _chiller_values(7.0, 12.0, 10.0),
              "c2": _chiller_values(7.0, 12.0, 10.0)}
    machines = [_machine("c1", "CH1", "chiller", cooling_w=800_000),
                _machine("c2", "CH2", "chiller", cooling_w=800_000),
                _machine("c3", "CH3", "chiller", cooling_w=800_000)]
    flags = {"c1": {"running": True, "alarm_points": []},
             "c2": {"running": True, "alarm_points": []},
             "c3": {"running": False, "alarm_points": []}}
    _patch(monkeypatch, machines, values=values, flags=flags)

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "chiller")
    assert stage["verdict"] == "n_plus_1"
    assert stage["running"] == 2 and stage["standby"] == 1
    # Capacity is the RUNNING set; installed carries the third machine.
    assert stage["capacity_kw"] == 1600.0
    assert stage["installed_kw"] == 2400.0


@pytest.mark.asyncio
async def test_one_machine_carrying_the_load_is_tight_not_ok(monkeypatch):
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000)],
           values={"c1": _chiller_values(7.0, 13.0, 20.0)},
           flags={"c1": {"running": True, "alarm_points": []}})

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "chiller")
    assert stage["verdict"] == "tight"
    assert "nothing on standby" in stage["why"]


@pytest.mark.asyncio
async def test_a_stage_with_nothing_running_says_so_first(monkeypatch):
    _patch(monkeypatch,
           [_machine("p1", "PUMP1", "pump"), _machine("p2", "PUMP2", "pump")],
           flags={"p1": {"running": False, "alarm_points": []},
                  "p2": {"running": False, "alarm_points": []}})

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "pump")
    assert stage["verdict"] == "no_capacity"


# --- the findings that only mean anything on a working machine ----------------

@pytest.mark.asyncio
async def test_low_delta_t_is_reported_on_a_circulating_loop(monkeypatch):
    """The classic chilled-water pathology: flow without heat transfer."""
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000)],
           values={"c1": _chiller_values(7.0, 8.5, 30.0)},
           flags={"c1": {"running": True, "alarm_points": []}})

    machine = (await plant.plant(_FakeSession()))["machines"][0]
    assert machine["verdict"] == "low_delta_t"
    assert "excess flow" in machine["why"]


@pytest.mark.asyncio
async def test_a_tower_is_judged_against_the_wet_bulb(monkeypatch):
    """Approach, not absolute temperature.

    15 C condenser water on a 10 C wet-bulb day is a good tower; the same
    15 C against a 5 C wet bulb is a tower that needs looking at.
    """
    _patch(monkeypatch,
           [_machine("t1", "CT1", "cooling_tower", room="Roof", power_w=30_000),
            _machine("t2", "CT2", "cooling_tower", room="Roof", power_w=30_000)],
           values={"t1": {("water_supply_temp", "COND"): 15.5,
                          ("water_return_temp", "COND"): 20.0,
                          ("outdoor_wet_bulb_temp", ""): 10.2,
                          ("fan_speed_pct", ""): 40.0},
                   "t2": {("water_supply_temp", "COND"): 15.5,
                          ("water_return_temp", "COND"): 20.0,
                          ("outdoor_wet_bulb_temp", ""): 5.0,
                          ("fan_speed_pct", ""): 100.0}},
           flags={"t1": {"running": True, "alarm_points": []},
                  "t2": {"running": True, "alarm_points": []}})

    by_name = {m["name"]: m for m in (await plant.plant(_FakeSession()))["machines"]}
    assert by_name["CT1"]["approach_k"] == 5.3
    assert by_name["CT1"]["verdict"] == "ok"
    assert by_name["CT2"]["verdict"] == "high_approach"
    assert "wet bulb" in by_name["CT2"]["why"]


@pytest.mark.asyncio
async def test_a_valve_that_is_not_obeying_is_an_actuator_fault(monkeypatch):
    """Commanded and measured are two points for exactly this reason."""
    _patch(monkeypatch,
           [_machine("v1", "VLV1", "valve")],
           values={"v1": {("valve_position_pct", "COMMANDED"): 65.0,
                          ("valve_position_pct", "MEASURED"): 20.0}},
           flags={"v1": {"running": True, "alarm_points": []}})

    machine = (await plant.plant(_FakeSession()))["machines"][0]
    assert machine["verdict"] == "actuator"
    assert machine["deviation_pct"] == 45.0


# --- the two readings of one load --------------------------------------------

@pytest.mark.asyncio
async def test_air_side_and_water_side_are_reported_separately(monkeypatch):
    """Both measure the same heat. Averaging them would hide a bad instrument."""
    _patch(monkeypatch,
           [_machine("a1", "CRAH1", "crah", room="Server Hall A",
                     cooling_w=100_000),
            _machine("c1", "CH1", "chiller", cooling_w=800_000)],
           values={"a1": {("cooling_output_pct", ""): 60.0,
                          ("supply_air_temp", ""): 22.0,
                          ("return_air_temp", ""): 27.0,
                          ("air_setpoint_temp", ""): 22.0},
                   "c1": _chiller_values(7.0, 13.0, 20.0)},
           flags={"a1": {"running": True, "alarm_points": []},
                  "c1": {"running": True, "alarm_points": []}})

    out = await plant.plant(_FakeSession())
    assert out["totals"]["air_load_kw"] == 60.0
    assert out["totals"]["water_load_kw"] == round(20.0 * 6.0 * 4.187, 1)
    assert any("apart" in n or "agreeing" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_nothing_reporting_is_empty_not_zero(monkeypatch):
    """A stage that published no power and a stage drawing none are different."""
    _patch(monkeypatch, [_machine("p1", "PUMP1", "pump")],
           flags={"p1": {"running": True, "alarm_points": []}})

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "pump")
    assert stage["power_kw"] is None
    assert stage["heat_kw"] is None


@pytest.mark.asyncio
async def test_a_machine_with_no_site_is_left_out(monkeypatch):
    """A plant table organised by site cannot place a machine that has none."""
    orphan = _machine("x1", "ORPHAN", "pump")
    orphan["site_id"] = None
    _patch(monkeypatch, [orphan])

    out = await plant.plant(_FakeSession())
    assert out["machines"] == [] and out["stages"] == []


@pytest.mark.asyncio
async def test_nameplate_comes_from_the_model_for_a_stopped_machine(monkeypatch):
    """The live capacity point reads zero when a machine stops.

    Reading redundancy off it would erase every standby machine exactly when
    somebody is asking whether standby exists.
    """
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000)],
           flags={"c1": {"running": False, "alarm_points": []}},
           observed={})

    stage = _stage(await plant.plant(_FakeSession()), "DC1", "chiller")
    assert stage["installed_kw"] == 800.0
    assert stage["capacity_kw"] is None


@pytest.mark.asyncio
async def test_the_chain_reads_in_the_direction_the_heat_travels(monkeypatch):
    """Order is the argument. Air first, sky last."""
    _patch(monkeypatch,
           [_machine("a1", "CRAH1", "crah", room="Server Hall A"),
            _machine("t1", "CT1", "cooling_tower", room="Roof"),
            _machine("c1", "CH1", "chiller"),
            _machine("p1", "PUMP1", "pump")],
           flags={k: {"running": True, "alarm_points": []}
                  for k in ("a1", "t1", "c1", "p1")})

    out = await plant.plant(_FakeSession())
    assert [s["stage"] for s in out["stages"]] == [
        "crah", "pump", "chiller", "cooling_tower"]


# --- the facility rooms -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_ups_on_battery_is_not_a_quiet_room(monkeypatch):
    """The worst thing in the room is the room's verdict.

    A verdict missing from the ranking folds to nothing and the room reads as
    healthy, which is exactly how a UPS sitting on battery would disappear.
    """
    _patch(monkeypatch,
           [_gear("u1", "UPS1", "ups"), _gear("u2", "UPS2", "ups")],
           values={"u1": {("load_pct", "OUTPUT"): 40.0,
                          ("power_draw", "OUTPUT"): 52_000.0,
                          ("battery_runtime", ""): 7200.0,
                          ("battery_health_pct", ""): 94.0}},
           flags={"u1": {"running": None, "alarm_points": [],
                         "states": {"On_Battery": True, "Bypass_Active": False}},
                  "u2": {"running": None, "alarm_points": [],
                         "states": {"On_Battery": False, "Bypass_Active": False}}})

    out = await plant.plant(_FakeSession())
    room = _room(out, "UPS Room")
    assert room["verdict"] == "on_battery"
    by_name = {m["name"]: m for m in out["equipment"]}
    assert by_name["UPS1"]["state_label"] == "On battery"
    assert "120 minutes" in by_name["UPS1"]["why"]
    assert by_name["UPS2"]["state_label"] == "On mains"
    assert by_name["UPS2"]["verdict"] == "ok"


@pytest.mark.asyncio
async def test_throughput_is_never_added_to_consumption(monkeypatch):
    """Three meters on one kilowatt is still one kilowatt.

    A UPS room meters the same power at the incoming feed, at the board and at
    the UPS output. Summing them would report three times what the room passes
    - and none of it is heat, which is the number beside it.
    """
    _patch(monkeypatch,
           [_gear("f1", "FEED1", "utility_feed"),
            _gear("s1", "SWGR1", "switchgear"),
            _gear("u1", "UPS1", "ups")],
           values={"f1": {("power_draw", ""): 147_000.0},
                   "s1": {("power_draw", ""): 68_000.0},
                   "u1": {("power_draw", "OUTPUT"): 52_000.0}},
           flags={"f1": {"running": None, "alarm_points": [],
                         "states": {"Service_Healthy": True}},
                  "s1": {"running": None, "alarm_points": [],
                         "states": {"Bus_Energized": True, "Breaker_Closed": True}},
                  "u1": {"running": None, "alarm_points": [], "states": {}}})

    room = _room(await plant.plant(_FakeSession()), "UPS Room")
    # One class only, the highest upstream one that reported.
    assert room["carried_kw"] == 147.0
    assert room["carried_by"] == "utility_feed"
    # Nothing in the room makes cold or consumes measured power of its own.
    assert room["heat_kw"] is None
    assert room["power_kw"] is None


@pytest.mark.asyncio
async def test_a_transferred_ats_and_a_dead_board_say_so(monkeypatch):
    """"Running" is the wrong word for most of the electrical spine."""
    _patch(monkeypatch,
           [_gear("a1", "ATS1", "ats"), _gear("b1", "SWGR1", "switchgear")],
           flags={"a1": {"running": None, "alarm_points": [],
                         "states": {"On_Emergency": True,
                                    "Normal_Available": False,
                                    "Emergency_Available": True}},
                  "b1": {"running": None, "alarm_points": [],
                         "states": {"Bus_Energized": False}}})

    by_name = {m["name"]: m for m in (await plant.plant(_FakeSession()))["equipment"]}
    assert by_name["ATS1"]["state_label"] == "On generator"
    assert by_name["ATS1"]["verdict"] == "on_emergency"
    assert by_name["SWGR1"]["state_label"] == "Dead"
    assert by_name["SWGR1"]["verdict"] == "de_energised"


@pytest.mark.asyncio
async def test_a_generator_running_is_worth_saying_out_loud(monkeypatch):
    """On test or on load, somebody should know which."""
    _patch(monkeypatch,
           [_gear("g1", "GEN1", "generator", room="Generator Room", room_id="r3"),
            _gear("g2", "GEN2", "generator", room="Generator Room", room_id="r3")],
           values={"g1": {("fuel_level_pct", ""): 83.0, ("load_pct", ""): 0.0},
                   "g2": {("fuel_level_pct", ""): 18.0}},
           flags={"g1": {"running": None, "alarm_points": [],
                         "states": {"Engine_Running": True}},
                  "g2": {"running": None, "alarm_points": [],
                         "states": {"Engine_Running": False}}})

    by_name = {m["name"]: m for m in (await plant.plant(_FakeSession()))["equipment"]}
    assert by_name["GEN1"]["verdict"] == "engine_running"
    assert by_name["GEN2"]["verdict"] == "low_fuel"
    assert "order a delivery" in by_name["GEN2"]["why"]


@pytest.mark.asyncio
async def test_power_quality_findings_use_the_published_limits(monkeypatch):
    """THD against IEEE 519, imbalance against what derates a motor."""
    _patch(monkeypatch,
           [_gear("m1", "MTR1", "energy_monitor", room="Mechanical Room", room_id="r4"),
            _gear("m2", "MTR2", "energy_monitor", room="Mechanical Room", room_id="r4")],
           values={"m1": {("voltage_thd_pct", ""): 7.4,
                          ("power_draw", ""): 34_000.0},
                   "m2": {("voltage_thd_pct", ""): 2.1,
                          ("phase_imbalance_pct", ""): 4.5}},
           flags={})

    by_name = {m["name"]: m for m in (await plant.plant(_FakeSession()))["equipment"]}
    assert by_name["MTR1"]["verdict"] == "distortion"
    assert "IEEE 519" in by_name["MTR1"]["why"]
    assert by_name["MTR2"]["verdict"] == "imbalance"


@pytest.mark.asyncio
async def test_the_chain_stays_cooling_only(monkeypatch):
    """A switchboard is not a stage of the cooling chain.

    The PLANT tab's stages, machines and totals are the machines that move
    heat. The room view is everything standing in the room, which is a
    different question with a different answer.
    """
    _patch(monkeypatch,
           [_machine("c1", "CH1", "chiller", cooling_w=800_000,
                     room_class="facility"),
            _gear("s1", "SWGR1", "switchgear", room="Central Plant", room_id="r1",
                  room_type="plant")],
           flags={"c1": {"running": True, "alarm_points": [], "states": {}},
                  "s1": {"running": None, "alarm_points": [],
                         "states": {"Bus_Energized": True}}})

    out = await plant.plant(_FakeSession())
    assert [m["name"] for m in out["machines"]] == ["CH1"]
    assert out["totals"]["machines"] == 1
    assert {s["stage"] for s in out["stages"]} == {"chiller"}
    # The room holds both.
    room = _room(out, "Central Plant")
    assert room["equipment"] == 2
    assert room["cooling_machines"] == 1
    assert sorted(m["name"] for m in out["equipment"]) == ["CH1", "SWGR1"]


@pytest.mark.asyncio
async def test_a_room_with_no_racks_and_no_class_is_not_a_facility_room(monkeypatch):
    """Absence of a classification is not a classification.

    Hiding or promoting a room on the strength of a missing field is how a
    real hall disappears from the estate view.
    """
    _patch(monkeypatch, [_machine("c1", "CH1", "chiller", cooling_w=800_000)],
           flags={"c1": {"running": True, "alarm_points": [], "states": {}}})

    assert (await plant.plant(_FakeSession()))["facility_rooms"] == []


@pytest.mark.asyncio
async def test_gear_with_no_reader_of_its_own_is_still_listed(monkeypatch):
    """A BACnet router that stops talking takes the plant off the page with it.

    Which looks like a plant failure and is not one - so it is listed, in the
    room where somebody would go to look at it.
    """
    _patch(monkeypatch,
           [_gear("r1", "BR1", "bacnet_router", room="Central Plant",
                  room_id="r1", room_type="plant"),
            dict(_gear("r2", "BR2", "bacnet_router", room="Central Plant",
                       room_id="r1", room_type="plant"), status="OFFLINE"),
            _gear("o1", "OOB1", "oob_switch", room="Central Plant",
                  room_id="r1", room_type="plant"),
            dict(_gear("p1", "PANEL", "rpp", room="Central Plant",
                       room_id="r1", room_type="plant"),
                 status="UNKNOWN", endpoints=0)],
           values={"o1": {("component_temperature", "CHASSIS"): 24.9}},
           flags={})

    out = await plant.plant(_FakeSession())
    room = _room(out, "Central Plant")
    assert room["equipment"] == 4
    # The only temperature a plant room has is a chassis, and it is named as
    # one rather than offered as room air - a chassis runs warmer than the
    # room it stands in.
    assert room["temp_c"] == 24.9
    assert room["temp_source"] == "chassis"
    by_name = {m["name"]: m for m in out["equipment"]}
    # Online with nothing this view charts is not silence. Calling it silence
    # would bury the one machine below that really has stopped talking.
    assert by_name["BR1"]["verdict"] == "ok"
    assert "stands in the room" in by_name["BR1"]["why"]
    # Polled and not answering.
    assert by_name["BR2"]["verdict"] == "silent"
    # Never polled. It cannot have gone quiet, so it does not get the word
    # that means something went quiet - and it does not drag the room down.
    assert by_name["PANEL"]["verdict"] == "unmonitored"
    assert room["unmonitored"] == 1
    assert room["verdict"] != "unmonitored"


# --- what a room row counts, and what it judges --------------------------------

@pytest.mark.asyncio
async def test_a_staged_off_machine_is_counted_not_judged(monkeypatch):
    """"Standby" was the verdict on three of five healthy facility rooms.

    A chiller plant is built to run fewer machines than it owns, so a room whose
    worst finding is "one of them is deliberately off" has nothing wrong in it.
    Saying so in a verdict column teaches the reader the column rarely means
    anything - so the split is counted instead, and the verdict is left for
    findings.
    """
    _patch(monkeypatch,
           [_gear("c1", "CHL1", "chiller", room="Central Plant", room_id="r1",
                  room_type="plant"),
            _gear("c2", "CHL2", "chiller", room="Central Plant", room_id="r1",
                  room_type="plant"),
            _gear("p1", "CHWP1", "pump", room="Central Plant", room_id="r1",
                  room_type="plant"),
            _gear("m1", "EV21", "energy_monitor", room="Central Plant",
                  room_id="r1", room_type="plant")],
           values={"m1": {("power_draw", ""): 34_000.0}},
           flags={"c1": {"running": True, "alarm_points": [],
                         "states": {"Chiller_Running": True}},
                  "c2": {"running": False, "alarm_points": [],
                         "states": {"Chiller_Running": False}},
                  "p1": {"running": True, "alarm_points": [],
                         "states": {"Run_Status": True}}})

    room = _room(await plant.plant(_FakeSession()), "Central Plant")
    assert (room["active"], room["standby"], room["attention"]) == (2, 1, 0)
    # The meter publishes no run state; counting it as off would report a
    # working plant room as half dead.
    assert room["machines_stated"] == 3 and room["no_state"] == 1
    assert room["verdict"] == "ok"


@pytest.mark.asyncio
async def test_a_ups_on_battery_is_counted_apart_from_both(monkeypatch):
    """Neither working nor deliberately off, and no alarm rule covers it.

    On battery, on bypass, on generator and a dead bus are states the platform
    holds no rule for, so the alarm count reads zero through all of them. If the
    room row only counted working-vs-spare they would vanish.
    """
    _patch(monkeypatch,
           [_gear("u1", "UPSA", "ups"), _gear("u2", "UPSB", "ups"),
            _gear("f1", "UTIL1", "utility_feed")],
           flags={"u1": {"running": None, "alarm_points": [],
                         "states": {"On_Battery": True, "Bypass_Active": False}},
                  "u2": {"running": None, "alarm_points": [],
                         "states": {"On_Battery": False, "Bypass_Active": False}},
                  "f1": {"running": None, "alarm_points": [],
                         "states": {"Service_Healthy": True}}})

    room = _room(await plant.plant(_FakeSession()), "UPS Room")
    assert room["alarms_open"] == 0          # nothing raised for it
    assert room["attention"] == 1            # and it is still visible
    assert room["attention_names"] == ["UPSA"]
    assert (room["active"], room["standby"]) == (2, 0)
    assert room["verdict"] == "on_battery"


@pytest.mark.asyncio
async def test_a_room_of_meters_reports_no_run_state_at_all(monkeypatch):
    """A meter is read by what it measures, not by whether it is turning."""
    _patch(monkeypatch,
           [_gear("m1", "EV21", "energy_monitor", room="Mechanical Room",
                  room_id="r4"),
            _gear("m2", "EV22", "energy_monitor", room="Mechanical Room",
                  room_id="r4")],
           values={"m1": {("power_draw", ""): 34_000.0},
                   "m2": {("power_draw", ""): 12_000.0}})

    room = _room(await plant.plant(_FakeSession()), "Mechanical Room")
    assert room["machines_stated"] == 0
    assert room["no_state"] == 2
    assert room["active"] == 0


@pytest.mark.asyncio
async def test_a_board_dead_by_design_counts_as_spare_not_as_a_warning(monkeypatch):
    """A generator board is dead whenever its generators are on standby.

    Which is every day of a working year. The verdict already knew that; the
    state count did not, and put a warning on both healthy generator rooms.
    """
    _patch(monkeypatch,
           [_gear("g1", "GEN1", "generator", room="Generator Room", room_id="r3"),
            _gear("b1", "SWGR2", "switchgear", room="Generator Room", room_id="r3")],
           values={"g1": {("fuel_level_pct", ""): 83.0}},
           flags={"g1": {"running": None, "alarm_points": [],
                         "states": {"Engine_Running": False}},
                  "b1": {"running": None, "alarm_points": [],
                         "states": {"Bus_Energized": False,
                                    "Source_Generator": True}}})

    room = _room(await plant.plant(_FakeSession()), "Generator Room")
    assert (room["active"], room["standby"], room["attention"]) == (0, 2, 0)
    assert room["verdict"] == "ok"


@pytest.mark.asyncio
async def test_the_roof_reads_its_own_outdoor_air(monkeypatch):
    """A roof is outdoors, and the towers on it carry the site's outdoor sensor.

    A tower's whole job is approach to wet bulb, so its controller has outdoor
    air wired to it. For that room the reading is not a proxy for the air - it
    is the air, and the page was ignoring a measurement it already had.
    """
    _patch(monkeypatch,
           [_gear("t1", "CT1", "cooling_tower", room="Roof", room_id="r5",
                  room_type="plant")],
           values={"t1": {("water_supply_temp", "COND"): 15.5,
                          ("water_return_temp", "COND"): 20.5,
                          ("outdoor_dry_bulb_temp", ""): 16.2,
                          ("outdoor_wet_bulb_temp", ""): 10.2,
                          ("fan_speed_pct", ""): 30.0}},
           flags={"t1": {"running": True, "alarm_points": [],
                         "states": {"Fan_Status": True}}})

    room = _room(await plant.plant(_FakeSession()), "Roof")
    assert room["temp_c"] == 16.2
    assert room["temp_source"] == "outdoor air"


@pytest.mark.asyncio
async def test_a_room_sensor_still_beats_both(monkeypatch):
    """Order is by how close the instrument is to the air in the room."""
    _patch(monkeypatch,
           [_gear("s1", "SNS1", "sensor", room="Central Plant", room_id="r1",
                  room_type="plant"),
            _gear("o1", "OOB1", "oob_switch", room="Central Plant", room_id="r1",
                  room_type="plant")],
           values={"s1": {("ambient_temperature", ""): 21.4},
                   "o1": {("component_temperature", "CHASSIS"): 28.9}})

    room = _room(await plant.plant(_FakeSession()), "Central Plant")
    assert room["temp_c"] == 21.4
    assert room["temp_source"] == "room sensor"


@pytest.mark.asyncio
async def test_a_room_with_no_thermometer_says_nothing(monkeypatch):
    """A switchroom with no instrument in it has no temperature to report.

    Inventing one - from a motor winding, a battery, the room next door - would
    put a number on the page that no sensor in the building measured.
    """
    _patch(monkeypatch,
           [_gear("u1", "UPSA", "ups"), _gear("a1", "ATS1", "ats")],
           flags={"u1": {"running": None, "alarm_points": [], "states": {}},
                  "a1": {"running": None, "alarm_points": [], "states": {}}})

    room = _room(await plant.plant(_FakeSession()), "UPS Room")
    assert room["temp_c"] is None
    assert room["temp_source"] is None
