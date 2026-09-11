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
             room="Central Plant", cooling_w=None, power_w=None):
    return {
        "device_id": device_id, "name": name, "device_type": dtype,
        "status": "ONLINE", "room_id": "r1", "room_name": room,
        "site_id": site, "site_code": code, "site_name": code,
        "model_name": f"model-{dtype}", "rated_cooling_w": cooling_w,
        "rated_power_w": power_w,
    }


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
