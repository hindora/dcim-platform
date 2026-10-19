"""Device actions: who may do what, what it takes down, and what is sent."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import device_actions as da

PDU = {"id": "pdu1", "name": "PDUA-DC1-HB-R2-05", "device_type": "pdu",
       "lifecycle": "in_service", "vendor": "APC by Schneider Electric", "model": "APC AP8941"}
SRV = {"id": "srv1", "name": "SRV03-DC1-HB-R2-05", "device_type": "server",
       "lifecycle": "in_service", "vendor": "Dell Technologies", "model": "Dell PowerEdge R7525"}

SNMP_EP = {"id": "ep-snmp", "protocol": "snmp", "role": "native_card", "enabled": True,
           "admin_state": "enabled", "addressing": {}, "owner": "col-dc1-bms"}
RF_EP = {"id": "ep-rf", "protocol": "redfish", "role": "bmc", "enabled": True,
         "admin_state": "enabled", "addressing": {}, "owner": "col-dc1-oob"}


def _cord(feed_id, status="ONLINE"):
    return {"load_id": "x", "psu": 1, "feed_id": feed_id, "feed_name": feed_id,
            "feed_status": status}


def _loads():
    return [
        # outlet 1: a server on this strip AND on the B strip -> loses redundancy only
        {"outlet": 1, "connector": "C13", "branch": "1", "phase": "L1", "load_id": "srv1",
         "load_name": "SRV01", "load_type": "server", "psu": 1,
         "cords": [_cord("pdu1"), _cord("pduB")]},
        # outlet 2: its other cord is on THIS strip too -> the same failure -> dark
        {"outlet": 2, "connector": "C13", "branch": "1", "phase": "L1", "load_id": "srv2",
         "load_name": "SRV02", "load_type": "server", "psu": 1,
         "cords": [_cord("pdu1"), _cord("pdu1")]},
        # outlet 3: the other feed is already offline -> dark
        {"outlet": 3, "connector": "C13", "branch": "2", "phase": "L2", "load_id": "srv3",
         "load_name": "SRV03", "load_type": "server", "psu": 1,
         "cords": [_cord("pdu1"), _cord("pduB", "OFFLINE")]},
        # outlet 4: single-corded
        {"outlet": 4, "connector": "C13", "branch": "2", "phase": "L2", "load_id": "srv4",
         "load_name": "SRV04", "load_type": "server", "psu": 1, "cords": [_cord("pdu1")]},
        # outlet 5: nothing corded
        {"outlet": 5, "connector": "C13", "branch": "3", "phase": "L3", "load_id": None,
         "load_name": None, "load_type": None, "psu": None, "cords": []},
    ]


@pytest.fixture
def fake(monkeypatch):
    """The repository, replaced: one device, its endpoints and loads."""
    state = {"device": PDU, "eps": [SNMP_EP], "open": None, "created": [], "plan": {}}

    async def basics(_s, _id):
        return state["device"]

    async def eps(_s, _id):
        return state["eps"]

    async def loads(_s, _id):
        return _loads()

    async def open_(_s, _id):
        return state["open"]

    async def create(_s, *, collector_id, payload, actor):
        state["created"].append((collector_id, payload, actor))
        return "cmd-1"

    async def plan(_s):
        return state["plan"]

    monkeypatch.setattr(da, "ownership", plan)
    monkeypatch.setattr(da.repo, "device_basics", basics)
    monkeypatch.setattr(da.repo, "endpoints_with_owner", eps)
    monkeypatch.setattr(da.repo, "outlet_loads", loads)
    monkeypatch.setattr(da.repo, "open_action", open_)
    monkeypatch.setattr(da.repo, "create_action_command", create)
    return state


def run(coro):
    return asyncio.run(coro)


# -------------------------------------------------------------- authority

def test_restoring_power_is_an_operators_but_cutting_it_is_an_admins():
    assert da.allowed("operator", da.ACTIONS["outlet_on"])
    assert da.allowed("operator", da.ACTIONS["power_on"])
    assert da.allowed("operator", da.ACTIONS["graceful_restart"])
    for a in ("outlet_off", "outlet_cycle", "force_off", "force_restart", "power_cycle"):
        assert not da.allowed("operator", da.ACTIONS[a]), a
        assert da.allowed("admin", da.ACTIONS[a]), a
    assert not da.allowed("viewer", da.ACTIONS["outlet_on"])


def test_every_action_that_takes_something_down_demands_the_name_typed():
    for a in da.ACTIONS.values():
        if a.id in ("outlet_on", "power_on"):
            assert not a.confirm, a.id
        else:
            assert a.confirm, a.id


# ------------------------------------------------------------- the writes

def test_outlet_control_oids_come_from_each_vendors_mib():
    assert da.outlet_oid("APC by Schneider Electric", 12, "off") == (
        "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.12", 2)
    assert da.outlet_oid("APC by Schneider Electric", 3, "cycle")[1] == 3
    assert da.outlet_oid("Raritan", 24, "off") == ("1.3.6.1.4.1.13742.6.4.1.2.1.2.1.24", 0)
    assert da.outlet_oid("Raritan", 24, "on")[1] == 1
    assert da.outlet_oid("Raritan", 24, "cycle")[1] == 2
    with pytest.raises(da.ActionError, match="no known outlet-control MIB"):
        da.outlet_oid("Eaton", 1, "off")


def test_the_sku_decides_whether_an_outlet_has_a_relay():
    assert da.switching("APC AP8941") is True
    assert da.switching("Raritan PX2-5170CR") is True
    assert da.switching("APC AP8886") is False
    assert da.switching("Mystery PDU 9000") is None


# ------------------------------------------------------------ capabilities

def test_a_switched_strip_offers_outlet_actions_with_each_loads_impact(fake):
    cap = run(da.capabilities(None, "pdu1", "admin"))
    assert cap["available"] and cap["class"] == "pdu"
    assert {a["id"] for a in cap["actions"]} == {"outlet_on", "outlet_off", "outlet_cycle"}
    impact = {o["outlet"]: (o["load"] or {}).get("impact") for o in cap["outlets"]}
    assert impact == {1: "degraded", 2: "dark", 3: "dark", 4: "dark", 5: None}


def test_the_callers_role_marks_which_actions_are_theirs(fake):
    cap = run(da.capabilities(None, "pdu1", "operator"))
    allowed = {a["id"]: a["allowed"] for a in cap["actions"]}
    assert allowed == {"outlet_on": True, "outlet_off": False, "outlet_cycle": False}


def test_a_metered_strip_is_refused_up_front_not_offered_and_failed(fake):
    fake["device"] = PDU | {"model": "APC AP8886"}
    cap = run(da.capabilities(None, "pdu1", "admin"))
    assert not cap["available"] and "metered" in cap["reason"] and cap["actions"] == []


def test_the_live_plan_names_the_owner_when_nothing_has_been_recorded(fake):
    """The assigner's record is empty on a fleet that has never failed over, yet
    every endpoint is polled every minute by the collector the plan gives it."""
    fake["eps"] = [SNMP_EP | {"owner": None}]
    fake["plan"] = {"ep-snmp": "col-dc1-bms-2"}
    cap = run(da.capabilities(None, "pdu1", "admin"))
    assert cap["available"] and cap["endpoint"]["owner"] == "col-dc1-bms-2"
    run(da.submit(None, device_id="pdu1", action_id="outlet_on", outlet=4, confirm=None,
                  reason=None, actor="hari", role="operator"))
    assert fake["created"][0][0] == "col-dc1-bms-2"


def test_a_metered_strip_says_so_even_when_its_endpoint_has_no_owner(fake):
    fake["device"] = PDU | {"model": "APC AP8886"}
    fake["eps"] = [SNMP_EP | {"owner": None}]
    assert "metered" in run(da.capabilities(None, "pdu1", "admin"))["reason"]


def test_no_endpoint_owner_means_no_actions(fake):
    fake["eps"] = [SNMP_EP | {"owner": None}]
    assert "no collector owns" in run(da.capabilities(None, "pdu1", "admin"))["reason"]
    fake["eps"] = []
    assert "no enabled SNMP" in run(da.capabilities(None, "pdu1", "admin"))["reason"]


def test_a_server_acts_through_its_bmc_and_other_gear_has_no_actions(fake):
    fake["device"], fake["eps"] = SRV, [RF_EP]
    cap = run(da.capabilities(None, "srv1", "admin"))
    assert cap["available"] and {a["id"] for a in cap["actions"]} == {
        "power_on", "graceful_shutdown", "graceful_restart", "force_restart",
        "power_cycle", "force_off"}
    fake["device"] = SRV | {"device_type": "crah"}
    assert not run(da.capabilities(None, "srv1", "admin"))["available"]
    fake["device"] = SRV | {"lifecycle": "decommissioned"}
    assert "decommissioned" in run(da.capabilities(None, "srv1", "admin"))["reason"]


# ----------------------------------------------------------------- preview

def test_the_preview_says_dark_or_degraded_before_anything_is_done(fake):
    p = run(da.preview(None, "pdu1", "outlet_off", 1, "admin"))
    assert [x["name"] for x in p["degraded"]] == ["SRV01"] and p["dark"] == []
    p = run(da.preview(None, "pdu1", "outlet_off", 4, "admin"))
    assert [x["name"] for x in p["dark"]] == ["SRV04"] and p["degraded"] == []
    # two cords into one strip, and a feed already down, are not redundancy
    assert run(da.preview(None, "pdu1", "outlet_cycle", 2, "admin"))["dark"]
    assert run(da.preview(None, "pdu1", "outlet_off", 3, "admin"))["dark"]
    p = run(da.preview(None, "pdu1", "outlet_off", 5, "admin"))
    assert p["dark"] == [] and "Nothing is recorded" in p["note"]
    p = run(da.preview(None, "pdu1", "outlet_on", 4, "operator"))
    assert p["dark"] == [] and "regains" in p["note"]


def test_a_preview_for_a_missing_outlet_or_action_is_refused(fake):
    with pytest.raises(da.ActionError, match="no outlet 99"):
        run(da.preview(None, "pdu1", "outlet_off", 99, "admin"))
    with pytest.raises(da.ActionError, match="unknown action"):
        run(da.preview(None, "pdu1", "explode", 1, "admin"))


# ------------------------------------------------------------------ submit

def _submit(fake, **kw):
    args = {"device_id": "pdu1", "action_id": "outlet_off", "outlet": 4,
            "confirm": PDU["name"], "reason": "rack move", "actor": "hari", "role": "admin"}
    return run(da.submit(None, **(args | kw)))


def test_an_outlet_off_is_queued_for_the_owning_collector_with_the_right_oid(fake):
    r = _submit(fake)
    cid, payload, actor = fake["created"][0]
    assert cid == "col-dc1-bms" and actor == "hari" and r["command_id"] == "cmd-1"
    assert payload["op"] == "snmp_set" and payload["endpoint_id"] == "ep-snmp"
    assert payload["oid"] == "1.3.6.1.4.1.318.1.1.26.9.2.4.1.5.4" and payload["value"] == 2
    assert payload["device_id"] == "pdu1" and payload["reason"] == "rack move"


def test_a_server_restart_is_a_redfish_reset_with_the_resettype(fake):
    fake["device"], fake["eps"] = SRV, [RF_EP]
    _submit(fake, device_id="srv1", action_id="graceful_restart", outlet=None,
            confirm=SRV["name"], role="operator")
    cid, payload, _ = fake["created"][0]
    assert cid == "col-dc1-oob" and payload["op"] == "redfish_action"
    assert payload["action_path"] == "Actions/ComputerSystem.Reset"
    assert payload["body"] == {"ResetType": "GracefulRestart"}


def test_the_name_must_be_typed_for_anything_that_takes_a_device_down(fake):
    for bad in (None, "", "yes", "PDUA"):
        with pytest.raises(da.ActionError, match="type the device's name"):
            _submit(fake, confirm=bad)
    assert fake["created"] == []
    _submit(fake, confirm=PDU["name"].lower())          # case does not matter
    assert len(fake["created"]) == 1


def test_switching_on_needs_no_confirmation(fake):
    _submit(fake, action_id="outlet_on", confirm=None, role="operator")
    assert fake["created"][0][1]["value"] == 1


def test_an_operator_cannot_cut_power_and_is_told_what_it_would_take(fake):
    with pytest.raises(da.ActionError, match="needs admin access") as e:
        _submit(fake, role="operator")
    assert e.value.code == 403 and fake["created"] == []


def test_a_second_action_on_a_busy_device_waits(fake):
    fake["open"] = {"id": "earlier"}
    with pytest.raises(da.ActionError, match="still in progress") as e:
        _submit(fake)
    assert e.value.code == 409 and fake["created"] == []


def test_an_outlet_the_strip_does_not_have_is_refused(fake):
    with pytest.raises(da.ActionError, match="no outlet 99"):
        _submit(fake, outlet=99)
    with pytest.raises(da.ActionError, match="no outlet None"):
        _submit(fake, outlet=None)


# ---------------------------------------------------------------- plumbing

def test_the_command_table_accepts_the_new_kind_and_the_migration_reverses():
    mig = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
           / "0109_the_command_queue_carries_device_actions.py").read_text(encoding="utf-8")
    assert "'upgrade', 'device_action'" in mig
    assert mig.count('drop_constraint("collector_command_kind"') == 2   # up and down
    assert "DELETE FROM collector_command WHERE kind = 'device_action'" in mig


def test_pdu_snmp_endpoints_declare_where_writes_go():
    from app.importer.endpoints import derive_endpoints
    pdu = {"device_type": "pdu", "name": "PDUA", "ip_address": "10.52.12.26",
           "mgmt_ip": "10.52.12.26", "snmp_port": 161}
    eps = derive_endpoints(pdu)
    assert eps[0].addressing == {"set_port": 1161}
    # only a strip is ever written to; nothing else's endpoint changes
    sw = {"device_type": "switch", "name": "LF1", "ip_address": "10.51.0.1",
          "mgmt_ip": "10.51.0.1", "snmp_port": 161}
    assert derive_endpoints(sw)[0].addressing == {}
    assert derive_endpoints(pdu, snmp_set_port=None)[0].addressing == {}
