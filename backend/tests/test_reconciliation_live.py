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

    actions = await AlarmService().sweep_trap_reconciliation(s)

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

    await AlarmService().sweep_trap_reconciliation(s)

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

    await AlarmService().sweep_trap_reconciliation(s)

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
    assert mine[0]["instance"] == crah["instance"]
