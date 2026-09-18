"""Both ends of a broken cable, shown as one row without losing either.

The console showed a link_down from LF1 and nothing else, and the question was
why the far end was not also reporting. It was not: the far end had no power.
That case and the two-ended case must not look the same afterwards, which is
the whole reason these fold rather than merge.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alarms import correlation, link_correlation

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def db_session():
    """A session whose work is always rolled back.

    Every device, port, cable and alarm below is created by the test itself,
    so this needs a schema but not an imported fleet. Existing alarms are
    cleared inside the transaction so the live board cannot pair with the
    cables under test.
    """
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        try:
            await s.execute(text("UPDATE alarm SET state = 'CLEARED' "
                                 "WHERE state <> 'CLEARED'"))
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _device(session, name, device_type="switch"):
    return await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle)
        VALUES (:n, :t, 'in_service') RETURNING id::text
    """), {"n": name, "t": device_type})


async def _iface(session, device_id, name, if_index):
    return await session.scalar(text("""
        INSERT INTO interface (device_id, if_index, name, role)
        VALUES (CAST(:d AS uuid), :i, :n, 'data') RETURNING id::text
    """), {"d": device_id, "i": if_index, "n": name})


async def _link(session, a_dev, a_if, b_dev, b_if, layer="production"):
    return await session.scalar(text("""
        INSERT INTO connection (layer, link_type,
                                a_device_id, a_termination_type, a_termination_id,
                                b_device_id, b_termination_type, b_termination_id)
        VALUES (CAST(:l AS layer_t), 'ethernet',
                CAST(:ad AS uuid), 'interface', CAST(:ai AS uuid),
                CAST(:bd AS uuid), 'interface', CAST(:bi AS uuid))
        RETURNING id::text
    """), {"l": layer, "ad": a_dev, "ai": a_if, "bd": b_dev, "bi": b_if})


async def _alarm(session, device_id, instance, *, seconds_ago=0):
    return await session.scalar(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, message,
                           source, state, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), 'link_down', :i, 'MAJOR', 'Link Down',
                'snmp_trap', 'ACTIVE',
                now() - make_interval(secs => :ago),
                now() - make_interval(secs => :ago))
        RETURNING id::text
    """), {"d": device_id, "i": instance, "ago": seconds_ago})


async def _row(session, alarm_id):
    return (await session.execute(text("""
        SELECT is_symptom, root_cause_alarm_id::text AS root, message
          FROM alarm WHERE id = CAST(:id AS uuid)
    """), {"id": alarm_id})).mappings().one()


@pytest_asyncio.fixture
async def cable(db_session):
    """Two switches, one cable, ports named at each end."""
    lf = await _device(db_session, "LF1-TEST")
    sp = await _device(db_session, "SP2-TEST")
    lf_p = await _iface(db_session, lf, "GigabitEthernet0/5", 5)
    sp_p = await _iface(db_session, sp, "Ethernet1/1", 1)
    conn = await _link(db_session, lf, lf_p, sp, sp_p)
    return {"lf": lf, "sp": sp, "lf_port": "GigabitEthernet0/5",
            "sp_port": "Ethernet1/1", "conn": conn}


async def test_the_second_end_folds_under_the_first(db_session, cable):
    """One cable, one visible row - and the row names both ends."""
    first = await _alarm(db_session, cable["lf"], cable["lf_port"], seconds_ago=10)
    second = await _alarm(db_session, cable["sp"], cable["sp_port"])

    root = await link_correlation.pair_ends(
        db_session, alarm_id=second, device_id=cable["sp"],
        instance=cable["sp_port"])

    assert root and root["id"] == first
    assert (await _row(db_session, second))["is_symptom"] is True
    assert (await _row(db_session, second))["root"] == first

    kept = await _row(db_session, first)
    assert kept["is_symptom"] is False
    assert kept["message"] == (
        "Link down: LF1-TEST GigabitEthernet0/5 <-> SP2-TEST Ethernet1/1")


async def test_the_earlier_end_is_the_root_whichever_arrives_second(
        db_session, cable):
    """Arrival order at the receiver is not observation order on the wire.

    The far end can be processed first and still have seen the fault later.
    Rooting on first_seen keeps the operator reading the end that saw it
    happen, not the end whose trap won a race through the queue.
    """
    late = await _alarm(db_session, cable["sp"], cable["sp_port"])
    early = await _alarm(db_session, cable["lf"], cable["lf_port"], seconds_ago=30)

    root = await link_correlation.pair_ends(
        db_session, alarm_id=early, device_id=cable["lf"],
        instance=cable["lf_port"])

    # This end IS the root, so nothing is returned to fold it under...
    assert root is None
    # ...and the end that reported later is the one that got folded.
    assert (await _row(db_session, late))["root"] == early
    assert (await _row(db_session, early))["is_symptom"] is False


async def test_one_end_reporting_alone_stays_visible(db_session, cable):
    """The silence of the far end is the diagnosis, not a missing alarm.

    LF1 reported a port down and SRV05 said nothing because SRV05 had been
    de-energised. A design that merged both ends into a synthesised link alarm
    would render this identically to a cut cable.
    """
    alone = await _alarm(db_session, cable["lf"], cable["lf_port"])

    root = await link_correlation.pair_ends(
        db_session, alarm_id=alone, device_id=cable["lf"],
        instance=cable["lf_port"])

    assert root is None
    row = await _row(db_session, alone)
    assert row["is_symptom"] is False
    assert row["message"] == "Link Down"  # untouched: no second end to name


async def test_an_alarm_that_names_no_port_is_not_paired(db_session, cable):
    """Before the mapping carried ifDescr, every port shared one alarm.

    Such a row cannot be matched to a cable, and picking one of a switch's
    ports to blame would be worse than leaving it unpaired.
    """
    vague = await _alarm(db_session, cable["lf"], "")
    assert await link_correlation.pair_ends(
        db_session, alarm_id=vague, device_id=cable["lf"], instance="") is None
    assert await link_correlation.find_link(
        db_session, device_id=cable["lf"], instance="") is None


async def test_two_ports_on_one_switch_are_two_different_links(db_session, cable):
    """The bug the port instance exists to stop.

    An uplink down and a server port down on the same leaf are two faults on
    two cables. With no port on the alarm they shared one key, and the second
    only refreshed the first.
    """
    other = await _device(db_session, "SRV09-TEST", "server")
    lf_p2 = await _iface(db_session, cable["lf"], "GigabitEthernet0/9", 9)
    other_p = await _iface(db_session, other, "eth0", 0)
    await _link(db_session, cable["lf"], lf_p2, other, other_p)

    uplink = await _alarm(db_session, cable["lf"], cable["lf_port"])
    downlink = await _alarm(db_session, cable["lf"], "GigabitEthernet0/9")

    a = await link_correlation.find_link(
        db_session, device_id=cable["lf"], instance=cable["lf_port"])
    b = await link_correlation.find_link(
        db_session, device_id=cable["lf"], instance="GigabitEthernet0/9")
    assert a["connection_id"] != b["connection_id"]
    assert a["peer_device_id"] == cable["sp"]
    assert b["peer_device_id"] == other

    # Neither folds under the other: different cables, unrelated faults.
    assert await link_correlation.pair_ends(
        db_session, alarm_id=downlink, device_id=cable["lf"],
        instance="GigabitEthernet0/9") is None
    assert (await _row(db_session, uplink))["is_symptom"] is False


async def test_oper_state_follows_the_ends(db_session, cable):
    """The link table stops saying 'unknown' about every link it holds."""
    assert await db_session.scalar(text(
        "SELECT oper_state FROM connection WHERE id = CAST(:c AS uuid)"),
        {"c": cable["conn"]}) == "unknown"

    alarm = await _alarm(db_session, cable["lf"], cable["lf_port"])
    assert await link_correlation.refresh_link_state(
        db_session, device_id=cable["lf"], instance=cable["lf_port"]) == "down"

    await db_session.execute(text(
        "UPDATE alarm SET state = 'CLEARED' WHERE id = CAST(:id AS uuid)"),
        {"id": alarm})
    assert await link_correlation.refresh_link_state(
        db_session, device_id=cable["lf"], instance=cable["lf_port"]) == "up"


async def test_a_link_stays_down_while_either_end_still_reports(
        db_session, cable):
    """A dead end cannot participate in its own recovery.

    If one end recovers and the other is still down - or still dark - the
    cable is not carrying traffic, and the model must not say it is.
    """
    near = await _alarm(db_session, cable["lf"], cable["lf_port"], seconds_ago=10)
    await _alarm(db_session, cable["sp"], cable["sp_port"])

    await db_session.execute(text(
        "UPDATE alarm SET state = 'CLEARED' WHERE id = CAST(:id AS uuid)"),
        {"id": near})

    assert await link_correlation.refresh_link_state(
        db_session, device_id=cable["lf"], instance=cable["lf_port"]) == "down"


async def test_power_and_cooling_connections_are_not_links(db_session):
    """A cord and a pipe have no link state to report.

    Both terminate on things that are not ports, and a device_id/name lookup
    against the interface table must not stray onto them.
    """
    pdu = await _device(db_session, "PDUA-TEST", "pdu")
    srv = await _device(db_session, "SRV-TEST", "server")
    a = await _iface(db_session, pdu, "port1", 1)
    b = await _iface(db_session, srv, "eth0", 0)
    await _link(db_session, pdu, a, srv, b, layer="power")

    assert await link_correlation.find_link(
        db_session, device_id=pdu, instance="port1") is None


# --- a far end with no power -------------------------------------------------
#
# Live, 2026-09-18: both rack PDUs feeding LF1-DC1-HA-R2-02 tripped, and four
# spine ports facing it went down three seconds later. The console showed six
# roots for one incident - two trips and four link-downs - because nothing
# asked whether the far end of those cables still had power.


async def _feed(session, feeder, load, side):
    await session.execute(text("""
        INSERT INTO connection (layer, link_type, a_device_id, b_device_id,
                                redundancy_side)
        VALUES (CAST('power' AS layer_t), 'power_cord',
                CAST(:a AS uuid), CAST(:b AS uuid), :s)
    """), {"a": feeder, "b": load, "s": side})


async def _trip(session, device_id, instance=""):
    return await session.scalar(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, message,
                           source, state, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), 'breaker_tripped', :i, 'CRITICAL',
                'Breaker Tripped', 'snmp_trap', 'ACTIVE',
                now() - interval '5 seconds', now() - interval '5 seconds')
        RETURNING id::text
    """), {"d": device_id, "i": instance})


@pytest_asyncio.fixture
async def fed_cable(db_session, cable):
    """The cable above, with the leaf dual-corded to an A and a B strip."""
    pa = await _device(db_session, "PDUA-TEST", "pdu")
    pb = await _device(db_session, "PDUB-TEST", "pdu")
    await _feed(db_session, pa, cable["lf"], "A")
    await _feed(db_session, pb, cable["lf"], "B")
    return {**cable, "pdu_a": pa, "pdu_b": pb}


async def test_link_down_folds_when_the_far_end_lost_both_feeds(db_session, fed_cable):
    c = fed_cable
    root_a = await _trip(db_session, c["pdu_a"])
    await _trip(db_session, c["pdu_b"])
    spine = await _alarm(db_session, c["sp"], c["sp_port"])

    root = await correlation.correlate(
        db_session, alarm_id=spine, device_id=c["sp"],
        alarm_type="link_down", instance=c["sp_port"])

    assert root is not None, "the far end is dark; its power loss explains the port"
    assert root["alarm_type"] == "breaker_tripped"
    assert root["id"] == root_a, "nearest, then oldest"
    assert (await _row(db_session, spine))["is_symptom"] is True


async def test_link_down_stays_a_root_while_the_far_end_is_still_fed(
        db_session, fed_cable):
    """One strip gone, the other healthy: the leaf is up, so the cable is the
    question, and hiding the alarm under a PDU would hide it."""
    c = fed_cable
    await _trip(db_session, c["pdu_a"])
    spine = await _alarm(db_session, c["sp"], c["sp_port"])

    assert await correlation.correlate(
        db_session, alarm_id=spine, device_id=c["sp"],
        alarm_type="link_down", instance=c["sp_port"]) is None
    assert (await _row(db_session, spine))["is_symptom"] is False


async def test_link_down_with_a_powered_far_end_is_never_folded(db_session, fed_cable):
    c = fed_cable
    spine = await _alarm(db_session, c["sp"], c["sp_port"])
    assert await correlation.correlate(
        db_session, alarm_id=spine, device_id=c["sp"],
        alarm_type="link_down", instance=c["sp_port"]) is None


async def test_a_named_breaker_bank_does_not_take_the_whole_strip_dark(
        db_session, fed_cable):
    """A multi-bank rack PDU trips one bank at a time, and the model does not
    know which outlets are on which bank. A trip that names its breaker is not
    taken as de-energising everything corded to the strip."""
    c = fed_cable
    await _trip(db_session, c["pdu_a"], instance="Breaker 1")
    await _trip(db_session, c["pdu_b"], instance="Breaker 1")
    spine = await _alarm(db_session, c["sp"], c["sp_port"])

    assert await correlation.correlate(
        db_session, alarm_id=spine, device_id=c["sp"],
        alarm_type="link_down", instance=c["sp_port"]) is None


async def test_a_dark_load_is_explained_by_its_tripped_feeds(db_session, fed_cable):
    """The same rule for the load's own unreachable alarm: with both strips
    tripped it folds under a trip, where before only an UNREACHABLE feeder
    counted as a power root."""
    c = fed_cable
    await _trip(db_session, c["pdu_a"])
    await _trip(db_session, c["pdu_b"])
    unreachable = await db_session.scalar(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, message,
                           source, state, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), 'endpoint_unreachable', 'ep', 'MAJOR',
                'No response', 'comm', 'ACTIVE', now(), now())
        RETURNING id::text
    """), {"d": c["lf"]})

    root = await correlation.correlate(
        db_session, alarm_id=unreachable, device_id=c["lf"],
        alarm_type="endpoint_unreachable")
    assert root is not None and root["alarm_type"] == "breaker_tripped"


# --- the root that arrives last ----------------------------------------------
#
# Live, the second time: the four linkDown traps and both breaker traps landed
# within 0.2 s, link-downs first, on two workers. Raise-time correlation saw no
# root and left all four standing.


async def test_a_late_trip_adopts_the_link_down_raised_before_it(db_session, fed_cable):
    c = fed_cable
    spine = await _alarm(db_session, c["sp"], c["sp_port"])          # first
    await _trip(db_session, c["pdu_a"])
    await _trip(db_session, c["pdu_b"])                              # last

    adopted = await correlation.adopt_orphans(
        db_session, alarm_type="breaker_tripped", device_id=c["pdu_b"],
        instance="")

    assert [a["id"] for a in adopted] == [spine]
    assert (await _row(db_session, spine))["is_symptom"] is True


async def test_a_late_trip_on_one_side_adopts_nothing(db_session, fed_cable):
    """The leaf is still fed from B, so its neighbours' link-downs are not
    explained - the veto applies to adoption exactly as to raise time."""
    c = fed_cable
    spine = await _alarm(db_session, c["sp"], c["sp_port"])
    await _trip(db_session, c["pdu_a"])

    assert await correlation.adopt_orphans(
        db_session, alarm_type="breaker_tripped", device_id=c["pdu_a"],
        instance="") == []
    assert (await _row(db_session, spine))["is_symptom"] is False


async def test_a_late_trip_adopts_the_dark_loads_own_unreachable(db_session, fed_cable):
    c = fed_cable
    unreachable = await db_session.scalar(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, message,
                           source, state, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), 'endpoint_unreachable', 'ep', 'MAJOR',
                'No response', 'comm', 'ACTIVE', now(), now())
        RETURNING id::text
    """), {"d": c["lf"]})
    await _trip(db_session, c["pdu_a"])
    await _trip(db_session, c["pdu_b"])

    adopted = await correlation.adopt_orphans(
        db_session, alarm_type="breaker_tripped", device_id=c["pdu_a"],
        instance="")
    assert unreachable in {a["id"] for a in adopted}


async def test_the_sweep_finds_trips_that_raced_each_other(db_session, fed_cable):
    """Two trips on two workers: neither adopted at raise time. The sweep's
    worklist must hold both, and adoption from it must succeed."""
    c = fed_cable
    spine = await _alarm(db_session, c["sp"], c["sp_port"])
    ta = await _trip(db_session, c["pdu_a"])
    tb = await _trip(db_session, c["pdu_b"])

    roots = {r["id"] for r in await correlation.open_deenergising_roots(db_session)}
    assert {ta, tb} <= roots

    for r in await correlation.open_deenergising_roots(db_session):
        await correlation.adopt_orphans(
            db_session, alarm_type=r["alarm_type"], device_id=r["device_id"],
            instance=r["instance"])
    assert (await _row(db_session, spine))["is_symptom"] is True


async def test_a_named_bank_trip_adopts_nothing(db_session, fed_cable):
    c = fed_cable
    await _alarm(db_session, c["sp"], c["sp_port"])
    assert await correlation.adopt_orphans(
        db_session, alarm_type="breaker_tripped", device_id=c["pdu_a"],
        instance="Breaker 1") == []
