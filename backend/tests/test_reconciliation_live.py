"""Reconciliation against real alarm rows, not against the shape of its SQL.

Two defects shipped in one afternoon and both were found by a person looking at
a page, because every test here was a string match on a query. The queries were
right. What was wrong was what happened when a row met them:

* the sweep cleared instance "" for every candidate, which is correct for an
  alarm filed at the device and a silent miss for one filed against a point -
  clear matches on (device, alarm_type, instance), so it found nothing,
  reported nothing, and tried the same wrong key on the next pass;

* an alarm filed under the old instance could not be closed by the rule that
  raised it, because the platform had since moved that condition to file at
  the machine.

Both are invisible to a test that reads SQL and obvious to one that raises an
alarm, feeds it a reading and asks whether the row closed. These do that.

Skipped unless DCIM_TEST_DATABASE_URL points at a database with an imported
fleet, so CI - which has no devices - does not run them. Every test runs inside
a transaction that is rolled back, so it can be pointed at the live database
without leaving an alarm or a reading behind.

    DCIM_TEST_DATABASE_URL=postgresql+asyncpg://... pytest tests/test_reconciliation_live.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alarms import reconcile
from app.alarms.service import AlarmService

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        try:
            # Start from a quiet board: the live fleet carries real alarms and
            # a sweep acts on every candidate it finds, so without this a test
            # would be reading someone else's rows. Rolled back with the rest.
            await s.execute(text("UPDATE alarm SET state = 'CLEARED' "
                                 "WHERE state <> 'CLEARED'"))
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _crah(s) -> dict:
    """A CRAH that is currently publishing its run status."""
    row = (await s.execute(text("""
        SELECT DISTINCT ON (d.id) d.id::text AS id, d.name, tb.instance
          FROM device d
          JOIN telemetry_bool tb ON tb.device_id = d.id
          JOIN metric m ON m.id = tb.metric_id
         WHERE d.device_type = 'crah' AND m.key = 'equipment_state'
         ORDER BY d.id, tb.ts DESC
         LIMIT 1
    """))).mappings().first()
    assert row, "no CRAH is publishing a run status"
    return dict(row)


async def _state(s, device_id: str, instance: str, value: bool,
                 ago_s: int = 5) -> None:
    """Write a run-status reading, as the ingest worker would."""
    await s.execute(text("""
        INSERT INTO telemetry_bool (device_id, metric_id, instance, ts, value)
        SELECT CAST(:d AS uuid), m.id, :i, :t, :v
          FROM metric m WHERE m.key = 'equipment_state'
    """), {"d": device_id, "i": instance, "v": value,
           "t": datetime.now(UTC) - timedelta(seconds=ago_s)})


async def _raise(s, device_id: str, *, instance: str, source: str = "snmp_trap",
                 alarm_type: str = "plant_unit_stopped", ago_s: int = 0) -> str:
    at = datetime.now(UTC) - timedelta(seconds=ago_s)
    row = (await s.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state,
                           message, source, category, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), :ty, :i, CAST('MAJOR' AS severity_t),
                'ACTIVE', 'test', :src, 'cooling', :t, :t)
        RETURNING id::text
    """), {"d": device_id, "ty": alarm_type, "i": instance,
           "src": source, "t": at})).first()
    return row[0]


def _service() -> AlarmService:
    """The service with no Redis behind it.

    Redis holds the dwell counters the rule engine advances per sample. The
    reconciliation sweep reads alarms and telemetry and touches none of it, so
    a stub is honest here: if that ever stops being true, this raises rather
    than quietly passing on a half-wired service.
    """
    class _NoRedis:
        def __getattr__(self, name):
            raise AssertionError(
                f"the reconciliation sweep reached Redis for {name!r}")

    return AlarmService(_NoRedis())


async def _is_open(s, alarm_id: str) -> bool:
    return (await s.execute(text(
        "SELECT state <> 'CLEARED' FROM alarm WHERE id = CAST(:a AS uuid)"),
        {"a": alarm_id})).scalar()


# ── the sweep must close the row it found ────────────────────────────────────

async def test_a_recovered_unit_closes_the_alarm_filed_at_the_device(session):
    """The ordinary case, and the one that already worked."""
    s = session
    crah = await _crah(s)
    await _state(s, crah["id"], crah["instance"], True)
    alarm = await _raise(s, crah["id"], instance="")

    actions = await _service().sweep_trap_reconciliation(s)

    assert not await _is_open(s, alarm), (
        f"{crah['name']} is running and its alarm is still open; "
        f"the sweep took {len(actions)} action(s)")


async def test_a_recovered_unit_closes_an_alarm_filed_against_a_point(session):
    """The defect. An alarm filed under the point that revealed it - which is
    every alarm raised before the condition moved to the device, and every
    instance-scoped alarm there is - was looked up under "" and never found.
    The sweep reported nothing cleared and tried the same wrong key forever.
    """
    s = session
    crah = await _crah(s)
    await _state(s, crah["id"], crah["instance"], True)
    alarm = await _raise(s, crah["id"], instance=crah["instance"])

    await _service().sweep_trap_reconciliation(s)

    assert not await _is_open(s, alarm), (
        f"an alarm filed under {crah['instance']!r} survived the sweep; "
        "it was cleared under the wrong key")


async def test_a_stopped_unit_keeps_its_alarm(session):
    """The other direction, which matters more. The state still says the
    machine is off, so nothing here may close it - not the state arm, and not
    the ageing timer however long it has been quiet."""
    s = session
    crah = await _crah(s)
    await _state(s, crah["id"], crah["instance"], False)
    alarm = await _raise(s, crah["id"], instance="", ago_s=7200)

    await _service().sweep_trap_reconciliation(s)

    assert await _is_open(s, alarm), (
        f"{crah['name']} is still stopped and its alarm was cleared anyway")


async def test_the_ageing_timer_cannot_take_a_state_backed_alarm(session):
    """Two hours quiet, the device reporting throughout, and the state saying
    the condition holds. The timer is for what nothing can measure."""
    s = session
    crah = await _crah(s)
    await _state(s, crah["id"], crah["instance"], False)
    await _raise(s, crah["id"], instance="", ago_s=7200)

    aged = await reconcile.aged_out(s, grace_s=0)

    assert not [r for r in aged if r["device_name"] == crah["name"]], (
        "the timer offered up an alarm whose state still asserts it")


async def test_the_candidates_carry_the_instance_they_are_filed_under(session):
    """The field the clear needs. Without it the sweep cannot name the row it
    just decided to close."""
    s = session
    crah = await _crah(s)
    await _state(s, crah["id"], crah["instance"], True)
    await _raise(s, crah["id"], instance=crah["instance"])

    rows = await reconcile.state_settled(s)
    mine = [r for r in rows if r["device_name"] == crah["name"]]

    assert mine, "the state says running and nothing was offered for clearing"
    # The ALARM's key, which is what the clear must name - not the point's
    # name, and not the wildcard the map uses to mean "whatever this machine
    # calls its run status".
    assert mine[0]["instance"] == crah["instance"]
    assert mine[0]["point"] == crah["instance"]


# ── an alarm on a key the device no longer publishes ─────────────────────────

async def _probe(s) -> dict:
    """A sensor that is publishing ambient temperature right now."""
    row = (await s.execute(text("""
        SELECT DISTINCT ON (d.id) d.id::text AS id, d.name,
               coalesce(t.instance, '') AS instance
          FROM device d
          JOIN telemetry_sample t ON t.device_id = d.id
          JOIN metric m ON m.id = t.metric_id
         WHERE m.key = 'ambient_temperature'
           AND t.ts > now() - interval '20 minutes'
         ORDER BY d.id, t.ts DESC
         LIMIT 1
    """))).mappings().first()
    assert row, "no sensor is publishing an ambient temperature"
    return dict(row)


@pytest.mark.asyncio
async def test_an_alarm_on_a_retired_instance_is_swept(session):
    """The five-day alarm this path was written for.

    THR-DC1-CP raised ambient_temp_high under the blank instance a rack probe
    uses. Migration 0063 then moved plant-room transmitters to `ROOM`, so every
    reading after it landed on a different key: the rule engine owns threshold
    alarms and clears them from the next reading in the clear band, and no such
    reading could ever arrive. It sat ACTIVE on a healthy transmitter.
    """
    probe = await _probe(session)
    # Filed against an instance this device does not publish.
    retired = "RETIRED-INSTANCE" if probe["instance"] != "RETIRED-INSTANCE" else "OTHER"
    alarm_id = await _raise(session, probe["id"], instance=retired,
                            source="threshold", alarm_type="ambient_temp_high",
                            ago_s=3 * 86400)
    await session.execute(text(
        "UPDATE alarm SET metric_key = 'ambient_temperature', threshold = 27 "
        "WHERE id = CAST(:a AS uuid)"), {"a": alarm_id})

    found = {r["id"] for r in await reconcile.orphaned_key(session)}
    assert alarm_id in found, "an alarm no reading can reach was left open"

    await _service().sweep_trap_reconciliation(session)
    assert not await _is_open(session, alarm_id)


@pytest.mark.asyncio
async def test_a_key_that_is_merely_slow_is_left_alone(session):
    """The network profiles poll every 600 s. Two hours of silence is twelve
    missed polls, not a slow one - but a key that spoke five minutes ago is
    simply being polled, and taking its alarm would be the sweep inventing a
    recovery."""
    probe = await _probe(session)
    alarm_id = await _raise(session, probe["id"], instance=probe["instance"],
                            source="threshold", alarm_type="ambient_temp_high",
                            ago_s=3 * 86400)
    await session.execute(text(
        "UPDATE alarm SET metric_key = 'ambient_temperature', threshold = 27 "
        "WHERE id = CAST(:a AS uuid)"), {"a": alarm_id})

    found = {r["id"] for r in await reconcile.orphaned_key(session)}
    assert alarm_id not in found
    assert await _is_open(session, alarm_id)


@pytest.mark.asyncio
async def test_a_device_that_has_gone_dark_keeps_its_alarm(session):
    """The safety half, and the reason this is not just a timer. "This key
    stopped" and "this machine went dark" look identical from the alarm table,
    and only the first means the alarm is stranded rather than true."""
    probe = await _probe(session)
    alarm_id = await _raise(session, probe["id"], instance="RETIRED-INSTANCE",
                            source="threshold", alarm_type="ambient_temp_high",
                            ago_s=3 * 86400)
    await session.execute(text(
        "UPDATE alarm SET metric_key = 'ambient_temperature', threshold = 27 "
        "WHERE id = CAST(:a AS uuid)"), {"a": alarm_id})
    # Nothing from this device inside the freshness window: it is dark.
    rows = await reconcile.orphaned_key(session, fresh_s=1)
    assert alarm_id not in {r["id"] for r in rows}


# ── a restart is an event, not a condition ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_restart_ages_out_before_a_condition_would(session):
    """Twenty minutes quiet: past the event grace, inside the condition one.

    The restart closes; an unmeasured condition raised at the same moment on
    the same device does not - the short life is for point events only.
    """
    s = session
    dev = (await s.execute(text("""
        SELECT t.device_id::text FROM telemetry_sample t
         WHERE t.ts > now() - interval '5 minutes' LIMIT 1
    """))).scalar()
    assert dev, "no device is delivering telemetry"
    quiet = reconcile.EVENT_GRACE_S + 300
    assert quiet < reconcile.REASSERT_GRACE_S
    boot = await _raise(s, dev, instance="", alarm_type="device_restarted",
                        ago_s=quiet)
    held = await _raise(s, dev, instance="", alarm_type="a_condition_nothing_measures",
                        ago_s=quiet)

    aged = {r["id"] for r in await reconcile.aged_out(s)}
    assert boot in aged, "a finished restart was held for the condition timer"
    assert held not in aged, "a condition aged out on the event timer"


# ── one alarm type, several rules ────────────────────────────────────────────
#
# ambient_temp_high carries two rules on sensors, told apart by instance: the
# 27 C rack intake on a sensor's own reading ("") and the 35 C room limit on a
# plant-room transmitter (ROOM). Ranking readings per alarm dealt them out
# between the rules, each saw one of the two samples it needs, and a trap
# alarm whose clear was lost sat open for six days on a sensor reading 23.8 C.

async def _rack_sensor(s) -> dict:
    """A sensor publishing its own (instance "") ambient temperature now."""
    row = (await s.execute(text("""
        SELECT DISTINCT ON (d.id) d.id::text AS id, d.name
          FROM device d
          JOIN telemetry_sample t ON t.device_id = d.id
          JOIN metric m ON m.id = t.metric_id
         WHERE d.device_type = 'sensor' AND m.key = 'ambient_temperature'
           AND coalesce(t.instance, '') = ''
           AND t.ts > now() - interval '20 minutes'
         ORDER BY d.id, t.ts DESC
         LIMIT 1
    """))).mappings().first()
    assert row, "no rack sensor is publishing an ambient temperature"
    return dict(row)


async def _ambient(s, device_id: str, value: float, ahead_s: int) -> None:
    """A reading AHEAD of now, so it is the newest the query can rank - the
    live fleet keeps writing real ones into the same window."""
    await s.execute(text("""
        INSERT INTO telemetry_sample (ts, device_id, metric_id, instance, value, quality)
        SELECT :t, CAST(:d AS uuid), m.id, '', :v, 'GOOD'
          FROM metric m WHERE m.key = 'ambient_temperature'
    """), {"d": device_id, "v": value,
           "t": datetime.now(UTC) + timedelta(seconds=ahead_s)})


async def test_a_lost_clear_closes_on_a_type_with_two_rules(session):
    s = session
    probe = await _rack_sensor(s)
    alarm = await _raise(s, probe["id"], instance="1", alarm_type="ambient_temp_high",
                         ago_s=120)
    await _ambient(s, probe["id"], 23.5, ahead_s=5)
    await _ambient(s, probe["id"], 23.6, ahead_s=6)

    rows = [r for r in await reconcile.measured_clear(s) if r["id"] == alarm]
    assert rows, "two rules on the type, two clear readings, and nothing was offered"
    assert rows[0]["instance"] == "1", "the clear must name the trap's own instance"
    assert rows[0]["clear_threshold"] == 25, "the intake rule judges the intake reading"

    await _service().sweep_trap_reconciliation(s)
    assert not await _is_open(s, alarm), (
        f"{probe['name']} reads 23.6 C and its ambient_temp_high stayed open")


async def test_the_room_limit_cannot_close_what_the_intake_limit_upholds(session):
    """29 C is inside the 35 C room limit's clear band and outside the 27 C
    intake limit's. A rack sensor is held to the intake limit, so it stays."""
    s = session
    probe = await _rack_sensor(s)
    alarm = await _raise(s, probe["id"], instance="1", alarm_type="ambient_temp_high",
                         ago_s=120)
    await _ambient(s, probe["id"], 29.0, ahead_s=5)
    await _ambient(s, probe["id"], 29.1, ahead_s=6)

    assert not [r for r in await reconcile.measured_clear(s) if r["id"] == alarm]
    await _service().sweep_trap_reconciliation(s)
    assert await _is_open(s, alarm), "a rack still at 29 C had its alarm closed"


# ── a lost clear must always have a way back ─────────────────────────────────
#
# Nine (alarm, device type) pairs had none: a state-backed alarm read at a
# point name the machine does not use, or a rule-backed one whose rule watches
# a metric the machine does not report. The timer skipped both, so a dropped
# clear trap meant an alarm open forever.

async def _device(s, device_type: str) -> dict:
    """A device of this type that is delivering telemetry right now."""
    row = (await s.execute(text("""
        SELECT d.id::text AS id, d.name
          FROM device d
         WHERE d.device_type = :t
           AND EXISTS (SELECT 1 FROM telemetry_sample t
                        WHERE t.device_id = d.id AND t.ts > now() - interval '10 minutes')
         LIMIT 1
    """), {"t": device_type})).mappings().first()
    assert row, f"no {device_type} is reporting"
    return dict(row)


async def _point(s, device_id: str, metric: str, instance: str, value: bool) -> None:
    await s.execute(text("""
        INSERT INTO telemetry_bool (device_id, metric_id, instance, ts, value)
        SELECT CAST(:d AS uuid), m.id, :i, now(), :v FROM metric m WHERE m.key = :m
    """), {"d": device_id, "m": metric, "i": instance, "v": value})


async def test_a_pumps_fault_is_read_back_at_the_pumps_own_point(session):
    s = session
    pump = await _device(s, "pump")
    await _point(s, pump["id"], "alarm_state", "Alarm_Fault", False)
    alarm = await _raise(s, pump["id"], instance="Alarm_Fault", alarm_type="pump_fault")

    await _service().sweep_trap_reconciliation(s)
    assert not await _is_open(s, alarm), (
        f"{pump['name']} reads Alarm_Fault false and its pump_fault stayed open")


async def test_a_pump_still_faulted_keeps_its_alarm(session):
    s = session
    pump = await _device(s, "pump")
    await _point(s, pump["id"], "alarm_state", "Alarm_Fault", True)
    alarm = await _raise(s, pump["id"], instance="Alarm_Fault", alarm_type="pump_fault",
                         ago_s=7200)

    await _service().sweep_trap_reconciliation(s)
    assert await _is_open(s, alarm), "the pump still says faulted and the alarm was closed"


async def test_a_ups_frequency_alarm_with_no_point_ages_out(session):
    """A UPS publishes no Alarm_UnderFrequency (an energy monitor does), so
    nothing can read the condition back and the timer is the only way home."""
    s = session
    ups = await _device(s, "ups")
    alarm = await _raise(s, ups["id"], instance="", alarm_type="frequency_out_of_range",
                         ago_s=7200)

    assert [r for r in await reconcile.aged_out(s) if r["id"] == alarm], (
        "a UPS frequency alarm nothing can measure was not offered to the timer")


async def test_a_pdu_temperature_alarm_with_no_temperature_ages_out(session):
    """pdu_temp_high's rule watches ambient_temperature, which a rack PDU never
    reports, so the rule cannot decide it and must not block the timer."""
    s = session
    pdu = await _device(s, "pdu")
    alarm = await _raise(s, pdu["id"], instance="", alarm_type="pdu_temp_high", ago_s=7200)

    assert [r for r in await reconcile.aged_out(s) if r["id"] == alarm], (
        "a PDU temperature alarm nothing can measure was not offered to the timer")


@pytest.mark.asyncio
async def test_an_alarm_on_a_boolean_is_not_an_orphan(session):
    """A boolean lives in telemetry_bool, change-logged. Read from
    telemetry_sample alone, psu_state and bank_breaker_closed were never
    published, and every psu_failure and breaker_tripped was closed as
    orphaned within a minute of being raised (2026-10-28, live)."""
    row = (await session.execute(text("""
        SELECT DISTINCT ON (tb.device_id) tb.device_id::text AS id, tb.instance
          FROM telemetry_bool tb JOIN metric m ON m.id = tb.metric_id
         WHERE m.key = 'psu_state' AND tb.ts > now() - interval '1 hour'
         ORDER BY tb.device_id, tb.ts DESC LIMIT 1
    """))).mappings().first()
    assert row, "no server is publishing psu_state"
    alarm_id = await _raise(session, row["id"], instance=row["instance"],
                            source="state", alarm_type="psu_failure")
    await session.execute(text(
        "UPDATE alarm SET metric_key = 'psu_state' WHERE id = CAST(:a AS uuid)"),
        {"a": alarm_id})
    found = {r["id"] for r in await reconcile.orphaned_key(session)}
    assert alarm_id not in found
