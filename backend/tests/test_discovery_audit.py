"""The discovery audit's memory: drift, the run snapshot, missing devices, schedules.

Identity drift is a pure function and is tested as one. The SQL is asserted on
the properties that make it SAFE - which is where the ways it can lie live: a
scope guard, a lifecycle guard, a protocol guard, and a lock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.repositories import discovery as repo
from app.services import discovery as svc

APP = Path(__file__).resolve().parents[1] / "app"
REPO = (APP / "repositories" / "discovery.py").read_text(encoding="utf-8")
SVC = (APP / "services" / "discovery.py").read_text(encoding="utf-8")
MIG = next((APP.parent / "alembic" / "versions").glob("0080_*.py")).read_text(
    encoding="utf-8")


def _body(src: str, name: str) -> str:
    start = src.index(f"async def {name}(")
    nxt = src.find("\nasync def ", start + 1)
    return src[start:nxt if nxt > 0 else len(src)]


# --------------------------------------------------------------- identity drift

def test_a_firmware_string_that_moved_is_a_change():
    old = {"sysDescr": "iDRAC9 6.10.30.00", "sysObjectID": ".1.3.6.1.4.1.674.10892.5"}
    new = {"sysDescr": "iDRAC9 7.10.30.00", "sysObjectID": "1.3.6.1.4.1.674.10892.5"}
    assert svc.identity_changes(old, "ABC1234", new, "ABC1234") == [
        ("sysDescr", "iDRAC9 6.10.30.00", "iDRAC9 7.10.30.00")]


def test_a_leading_dot_or_case_is_not_a_change():
    """".1.3.6..." and "1.3.6..." are one OID, and a serial read in another case is
    one serial - flagging either would bury the real changes in formatting."""
    old = {"sysObjectID": ".1.3.6.1.4.1.9.1.1"}
    new = {"sysObjectID": "1.3.6.1.4.1.9.1.1"}
    assert svc.identity_changes(old, "fdo2222", new, "FDO2222 ") == []


def test_a_partial_read_is_not_a_change():
    """A probe that dropped one varbind this time is a slow agent, not a swapped box.
    Recording "serial: X -> nothing" would flap on every timeout."""
    old = {"sysDescr": "Cisco IOS XE", "model": "C9300"}
    assert svc.identity_changes(old, "FDO1", {"sysDescr": "Cisco IOS XE"}, None) == []
    assert svc.identity_changes({}, None, {"sysDescr": "x"}, "FDO1") == []


def test_a_different_serial_at_the_same_address_is_hardware():
    diff = svc.identity_changes({}, "FDO1111", {}, "FDO2222")
    assert diff == [("serial", "FDO1111", "FDO2222")]
    assert "serial" in svc.HARDWARE_FIELDS


def test_firmware_and_names_are_not_hardware():
    """A fleet-wide BMC firmware roll would otherwise put three hundred rows in
    "needs action" overnight."""
    for soft in ("sysDescr", "redfishVersion", "hostName", "sysName"):
        assert soft not in svc.HARDWARE_FIELDS, soft
    for hard in ("serial", "sysObjectID", "uuid", "model", "vendor", "engineID"):
        assert hard in svc.HARDWARE_FIELDS, hard


V3 = {"version": "3", "credential": "pool", "port": "161"}


def test_a_new_snmpv3_engine_id_is_a_swapped_box():
    """An agent keeps its engine ID across reboots; a new one at the same address
    is a swapped card or a factory reset - and every SNMPv3 key is localised to it."""
    old = {"engineID": "80001F88010A340B0A", "access": V3}
    new = {"engineID": "80007736010a340b0a", "access": V3}
    assert svc.identity_changes(old, None, new, None) == [
        ("engineID", "80001f88010a340b0a", "80007736010a340b0a")]
    assert "engineID" in svc.HARDWARE_FIELDS


def test_an_engine_id_seen_over_v2c_is_not_compared():
    """A v2c answer from an agent that also speaks v3 carries an engine ID, but
    nothing depends on it - and some agents mint a new one every start (snmpsim's
    shared v2c engines do), which would read as a swapped box after each restart."""
    v2c = {"version": "2c", "credential": "address"}
    for old_access, new_access in ((v2c, v2c), (V3, v2c), (v2c, V3), (None, V3)):
        old = {"engineID": "80001f88aaaa", "access": old_access}
        new = {"engineID": "80001f88bbbb", "access": new_access}
        assert svc.identity_changes(old, None, new, None) == [], (old_access, new_access)


def test_the_same_engine_id_in_another_case_is_not_a_change():
    old = {"engineID": "80007736010A340B0A", "access": V3}
    new = {"engineID": "80007736010a340b0a", "access": V3}
    assert svc.identity_changes(old, None, new, None) == []


# ------------------------------------------------------- shared addresses

def _row(name, via, protocol=None):
    return {"device_id": f"id-{name}", "name": name, "via": via, "protocol": protocol}


#: A BACnet/IP-to-MS/TP router and the trunk it fronts: the field devices are
#: polled at the router's IP. Found live 2026-10-06, the router's SNMP answer filed
#: against a pump because the address map kept whichever row came last.
ROUTER_AND_TRUNK = [
    *(_row(f"CHWP{i}-DC1-CP", "endpoint", "bacnet") for i in (4, 1, 3, 2)),
    _row("BRTR1-DC1-CP", "endpoint", "snmp"),
    _row("BRTR1-DC1-CP", "mgmt"),
    _row("VCW-DC1-CP", "endpoint", "bacnet"),
]


def test_an_answer_goes_to_the_device_polled_on_its_protocol():
    assert repo.pick_match(ROUTER_AND_TRUNK, "snmp")["name"] == "BRTR1-DC1-CP"


def test_without_a_protocol_match_the_management_address_decides():
    rows = [r for r in ROUTER_AND_TRUNK if r["protocol"] != "snmp"]
    assert repo.pick_match(rows, "redfish")["name"] == "BRTR1-DC1-CP"


def test_an_ambiguous_address_gets_the_same_answer_every_time():
    """Never "whichever row came last": the same sweep must file the same answer
    against the same device, or changes and alarms wander between machines."""
    rows = [_row(n, "endpoint", "bacnet") for n in ("PUMP-B", "PUMP-A", "PUMP-C")]
    picks = {repo.pick_match(list(reversed(rows)) if i % 2 else rows, "snmp")["name"]
             for i in range(6)}
    assert picks == {"PUMP-A"}
    assert repo.pick_match([], "snmp") is None and repo.pick_match(None, "snmp") is None


def test_record_results_chooses_by_protocol():
    body = _body(SVC, "record_results")
    assert "repo.pick_match(known.get(addr), protocol)" in body
    assert body.index('protocol = r.get("protocol")') < body.index("repo.pick_match(")


def test_changes_are_measured_against_what_was_there_before_the_upsert():
    body = _body(SVC, "record_results")
    assert body.index("prior_identities") < body.index("upsert_candidate"), (
        "the previous identity must be read before this run overwrites it")
    assert "record_changes" in body


# -------------------------------------------------------------- run snapshot

def test_a_run_records_what_it_concluded_when_it_finished():
    """Computed live, a run's counts followed each candidate's CURRENT run_id,
    which the next sweep of the range moves - so history shrank to zero the moment
    a range was re-swept."""
    finish = _body(REPO, "finish_run")
    for col in ("known", "unknown", "moved", "with_serial", "appeared", "gone",
                "changed"):
        assert f"{col} = :{col}" in finish, col
    runs = _body(REPO, "list_runs")
    assert "COALESCE(r.known, c.known, 0)" in runs, "the snapshot must win"


def test_appearance_is_counted_from_the_upsert_itself():
    """xmax = 0 is the row this statement inserted - and one that was gone and is
    answering again counts too."""
    assert "(xmax = 0) AS inserted" in REPO
    assert 'before["status"] == "gone"' in SVC


def test_silence_is_judged_from_when_the_sweep_began_asking():
    """Queued is not started. With schedules a run can wait behind another, and
    judging from the queue time under-marks what went quiet."""
    assert "claimed_at = now()" in _body(REPO, "claim_pending")
    assert "COALESCE(claimed_at, started_at)" in _body(REPO, "mark_gone")


# ------------------------------------------------------------ missing devices

def test_missing_is_judged_only_where_a_sweep_looked():
    body = _body(REPO, "missing_devices")
    assert '_probed("a.address", "s.cidr")' in body, "only what the sweep probed"
    assert "status = 'done'" in body
    # The LATEST covering run, per device.
    assert "DISTINCT ON (a.device_id)" in body and "finished_at DESC" in body


def test_a_device_the_sweep_cannot_speak_to_is_never_missing():
    """A chiller behind a BACnet router cannot answer an SNMP sweep however healthy
    it is. Counting it missing would fill the page with false alarms."""
    assert repo.SWEPT_PROTOCOLS == ("snmp", "redfish")
    assert "e.protocol::text = ANY(:protocols)" in _body(REPO, "missing_devices")


def test_only_hardware_that_should_be_on_the_wire_is_missing():
    """Planned and in-stock boxes are not racked yet; decommissioned ones are not
    supposed to answer."""
    assert set(repo.EXPECTED_ON_WIRE) == {"installed", "in_service", "maintenance"}
    assert "d.lifecycle::text = ANY(:lifecycles)" in _body(REPO, "missing_devices")


def test_answering_anywhere_is_not_missing():
    """Matched by serial at another address is MOVED - that finding exists already."""
    body = _body(REPO, "missing_devices")
    assert "c.matched_device_id = d.id" in body
    assert "c.last_seen >= cv.began" in body


def test_missing_carries_the_polling_state():
    """Silent to the sweep but ONLINE to the poller is a credentials or ACL
    problem - the disagreement is the diagnosis."""
    assert "any_online" in _body(REPO, "missing_devices")


# ------------------------------------------------------------------ schedules

def test_a_schedule_is_a_real_interval_not_any_integer():
    assert svc.SCHEDULE_INTERVALS == (6, 12, 24, 48, 168)
    with pytest.raises(svc.DiscoveryError):
        asyncio.run(svc.create_schedule(
            None, name=None, range_ids=["0b1c2d3e-0000-4000-8000-000000000001"],
            interval_hours=1))


@pytest.mark.parametrize("bad", [[], [""], ["10.0.0.0/24"], ["banana"]])
def test_a_schedule_sweeps_saved_ranges_not_cidr_text(bad):
    """Editing a range must change what its schedules audit. A copy of the CIDR
    text would go on sweeping the old one."""
    with pytest.raises(svc.DiscoveryError):
        svc._validate_range_ids(bad)


def test_a_due_schedule_waits_for_a_sweep_in_flight():
    """One sweep at a time. A due schedule that finds one running stays due and
    fires late, rather than stacking a second sweep on the first."""
    body = _body(SVC, "fire_due_schedule")
    # Claimed, then checked against ITS collectors, then queued.
    assert body.index("claim_due_schedules") < body.index("run_in_flight(session, lanes)")
    assert body.index("run_in_flight(session, lanes)") < body.index("discovery_ranges.queue")


def test_two_api_processes_cannot_fire_one_schedule_twice():
    assert "FOR UPDATE SKIP LOCKED" in _body(REPO, "claim_due_schedules")


def test_a_missed_tick_runs_late_once_rather_than_catching_up():
    """Advancing from the OLD due time would, after an outage, fire once for every
    interval missed."""
    from datetime import UTC, datetime, timedelta

    from app.services import schedule_time as st
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert st.next_after({"interval_hours": 24}, now) == now + timedelta(hours=24)
    fire = _body(SVC, "fire_due_schedule")
    assert "schedule_time.next_after(sched, datetime.now(UTC))" in fire


def test_the_migration_guards_the_schedule():
    assert "interval_hours >= 1" in MIG
    assert "cardinality(subnets) >= 1" in MIG


def test_the_scheduler_does_not_touch_the_database_on_startup():
    """Tests open the app for one request through the lifespan; a scheduler that
    wrote on its first tick would find their database."""
    from app.services import discovery_scheduler as sch
    assert sch.FIRST_TICK_S >= 10


def test_a_sweep_never_covers_an_address_it_did_not_probe():
    """Found live: two devices on .31 read as missing after a /27 sweep. .31 is that
    /27's broadcast address, which the sweeper skips - it never sent them a packet.
    Coverage now excludes network and broadcast below /31, exactly as Hosts() does,
    for missing devices AND for marking responders gone."""
    frag = repo._probed("a", "c")
    assert "host(network(CAST(c AS inet)))" in frag
    assert "host(broadcast(CAST(c AS inet)))" in frag, "mask-free comparison"
    assert "masklen(CAST(c AS inet)) >= 31" in frag, "/31 and /32 have neither to skip"
    assert '_probed("address", "s.cidr")' in _body(REPO, "mark_gone")


def test_a_run_keeps_its_origin_when_its_schedule_is_deleted():
    """Found live: deleting a schedule nulled schedule_id on its runs (it must -
    a run outlives what queued it), and that was the only trace a schedule had
    asked, so its history turned into hand-run sweeps."""
    create = _body(REPO, "create_run")
    assert '"schedule" if schedule_id else "manual"' in create
    assert "schedule_label" in create
    assert "COALESCE(sch.name, r.schedule_label)" in _body(REPO, "list_runs")
    assert "schedule_label=" in _body(SVC, "fire_due_schedule")
    mig = next((APP.parent / "alembic" / "versions").glob("0081_*.py")).read_text(
        encoding="utf-8")
    assert "trigger IN ('manual', 'schedule')" in mig


# ------------------------------------------------------------------ nav badge

def test_the_badge_counts_what_the_page_opens_on():
    """The badge said "0" while the page opened on devices that stopped answering:
    it counted unrecorded and moved, and missing and replaced arrived after it."""
    src = (APP / "repositories" / "assets.py").read_text(encoding="utf-8")
    assert 'attention["missing"] + attention["replaced"]' in src


def test_maintenance_is_listed_missing_but_not_badged():
    """A device in maintenance is expected to go quiet. The page lists it, and
    nobody is asked to act on it."""
    body = _body(REPO, "attention_counts")
    assert 'm["lifecycle"] != "maintenance"' in body
    assert "maintenance" in repo.EXPECTED_ON_WIRE, "still listed on the page"


def test_replaced_is_hardware_and_unacknowledged():
    body = _body(REPO, "attention_counts")
    assert "ch.acknowledged_at IS NULL" in body
    assert "sorted(HARDWARE_FIELDS)" in body
    assert svc.HARDWARE_FIELDS is repo.HARDWARE_FIELDS, "one definition"
