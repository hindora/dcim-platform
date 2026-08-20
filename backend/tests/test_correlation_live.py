"""Correlation against the real topology.

Skipped unless DCIM_TEST_DATABASE_URL points at a database with an imported
fleet, so CI (which has no device data) does not run it. Every test opens a
transaction and rolls it back, so it can be run against the live database
without leaving an alarm behind.

    DCIM_TEST_DATABASE_URL=postgresql+asyncpg://... pytest tests/test_correlation_live.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alarms import correlation

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    """A session whose work is always rolled back."""
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        try:
            # Start from a quiet board. The live fleet carries real alarms, and
            # without this the engine correlates against those instead of the
            # ones each test sets up - which made two of these tests fail for
            # reasons that had nothing to do with the code under test.
            # Rolled back with everything else, so the real alarms are untouched.
            await s.execute(text("UPDATE alarm SET state = 'CLEARED' "
                                 "WHERE state <> 'CLEARED'"))
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _raise(s, device_id: str, instance: str = "test") -> str:
    row = (await s.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state,
                           message, source, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), 'endpoint_unreachable', :i,
                CAST('MAJOR' AS severity_t), 'ACTIVE', 'test', 'comm', :t, :t)
        RETURNING id::text
    """), {"d": device_id, "i": instance, "t": datetime.now(UTC)})).first()
    return row[0]


async def test_oob_switch_failure_yields_one_root_and_n_symptoms(session):
    """The exit criterion: a dead management switch is ONE incident.

    Every device whose management interface lands on it stops answering. The
    devices are fine - the path used to watch them is gone - so presenting 60
    equal alarms buries the one an operator can act on.
    """
    s = session
    sw = (await s.execute(text("""
        SELECT c.b_device_id::text AS id, d.name, count(*) AS behind
          FROM connection c JOIN device d ON d.id = c.b_device_id
         WHERE c.layer = CAST('management' AS layer_t)
         GROUP BY 1, 2 HAVING count(*) >= 5
         ORDER BY 3 DESC LIMIT 1
    """))).mappings().first()
    assert sw, "no OOB switch with devices behind it"

    behind = (await s.execute(text("""
        SELECT DISTINCT c.a_device_id::text AS id
          FROM connection c
         WHERE c.layer = CAST('management' AS layer_t)
           AND c.b_device_id = CAST(:sw AS uuid)
           AND c.a_device_id <> CAST(:sw AS uuid)
         LIMIT 20
    """), {"sw": sw["id"]})).scalars().all()

    root_id = await _raise(s, sw["id"])           # the switch dies
    suppressed = 0
    for dev in behind:                            # everything behind it follows
        aid = await _raise(s, dev)
        if await correlation.correlate(
                s, alarm_id=aid, device_id=dev,
                alarm_type="endpoint_unreachable"):
            suppressed += 1

    roots = (await s.execute(text("""
        SELECT count(*) FROM alarm
         WHERE state <> 'CLEARED' AND NOT is_symptom
           AND root_cause_alarm_id IS NULL AND id = CAST(:r AS uuid)
    """), {"r": root_id})).scalar()

    assert roots == 1, "the switch's own alarm must stay a root"
    assert suppressed == len(behind), (
        f"{len(behind) - suppressed} of {len(behind)} devices behind "
        f"{sw['name']} were not folded under it")
    print(f"\n  {sw['name']}: 1 root + {suppressed} symptoms")


async def test_a_feed_failure_with_healthy_b_is_not_suppressed(session):
    """The other half of the criterion, and the one that can do harm.

    Suppressing a load's alarm because its A feed failed - while B is fine -
    hides a real fault AND hides that the load is now running unprotected.
    """
    s = session
    load = (await s.execute(text("""
        SELECT c.b_device_id::text AS id, d.name,
               count(DISTINCT c.redundancy_side) AS sides
          FROM connection c JOIN device d ON d.id = c.b_device_id
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.redundancy_side IS NOT NULL
         GROUP BY 1, 2 HAVING count(DISTINCT c.redundancy_side) >= 2
         LIMIT 1
    """))).mappings().first()
    assert load, "no dual-fed load found"

    a_feeder = (await s.execute(text("""
        SELECT DISTINCT c.a_device_id::text
          FROM connection c
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.b_device_id = CAST(:d AS uuid)
           AND c.redundancy_side = 'A' LIMIT 1
    """), {"d": load["id"]})).scalar()

    await _raise(s, a_feeder)                     # lose the A feed only
    sides = await correlation.feed_side_status(s, load["id"])
    assert sides.get("A") is True, "the A path should read as compromised"
    assert sides.get("B") is False, "the B path should still be healthy"
    assert correlation.has_surviving_feed(sides)

    aid = await _raise(s, load["id"])
    root = await correlation.correlate(
        s, alarm_id=aid, device_id=load["id"], alarm_type="endpoint_unreachable")
    assert root is None, (
        f"{load['name']} was suppressed under its A feed while B was healthy - "
        "that hides a real fault and a single-feed condition")
    print(f"\n  {load['name']}: A down, B healthy -> NOT suppressed ({sides})")


async def test_losing_both_feeds_does_suppress(session):
    """The mirror image: with no path left, the load really is explained."""
    s = session
    load = (await s.execute(text("""
        SELECT c.b_device_id::text AS id, d.name
          FROM connection c JOIN device d ON d.id = c.b_device_id
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.redundancy_side IS NOT NULL
         GROUP BY 1, 2 HAVING count(DISTINCT c.redundancy_side) >= 2
         LIMIT 1
    """))).mappings().first()

    feeders = (await s.execute(text("""
        SELECT DISTINCT c.a_device_id::text
          FROM connection c
         WHERE c.layer = CAST('power' AS layer_t)
           AND c.b_device_id = CAST(:d AS uuid)
    """), {"d": load["id"]})).scalars().all()
    for f in feeders:
        await _raise(s, f)

    sides = await correlation.feed_side_status(s, load["id"])
    assert not correlation.has_surviving_feed(sides), sides

    aid = await _raise(s, load["id"])
    root = await correlation.correlate(
        s, alarm_id=aid, device_id=load["id"], alarm_type="endpoint_unreachable")
    assert root is not None, "with every feed gone the load IS explained"
    print(f"\n  {load['name']}: all feeds down -> suppressed under "
          f"{root['device_name']}")


async def test_clearing_the_root_releases_its_symptoms(session):
    """A symptom must not stay hidden once its cause is fixed."""
    s = session
    sw = (await s.execute(text("""
        SELECT c.b_device_id::text AS id
          FROM connection c
         WHERE c.layer = CAST('management' AS layer_t)
         GROUP BY 1 HAVING count(*) >= 5 ORDER BY count(*) DESC LIMIT 1
    """))).scalar()
    dev = (await s.execute(text("""
        SELECT c.a_device_id::text FROM connection c
         WHERE c.layer = CAST('management' AS layer_t)
           AND c.b_device_id = CAST(:sw AS uuid)
           AND c.a_device_id <> CAST(:sw AS uuid) LIMIT 1
    """), {"sw": sw})).scalar()

    root_id = await _raise(s, sw)
    aid = await _raise(s, dev)
    assert await correlation.correlate(
        s, alarm_id=aid, device_id=dev, alarm_type="endpoint_unreachable")

    released = await correlation.release_symptoms(s, root_id)
    assert [r["id"] for r in released] == [aid]

    still = (await s.execute(text(
        "SELECT is_symptom FROM alarm WHERE id = CAST(:i AS uuid)"),
        {"i": aid})).scalar()
    assert still is False, "symptom stayed suppressed after its root cleared"


async def test_only_visibility_alarms_are_suppressible(session):
    """A hot CPU behind a dead switch is still a hot CPU.

    Folding a real condition about the device under a comms root would lose it.
    """
    s = session
    dev = (await s.execute(text("""
        SELECT c.a_device_id::text FROM connection c
         WHERE c.layer = CAST('management' AS layer_t) LIMIT 1
    """))).scalar()
    assert await correlation.correlate(
        s, alarm_id="00000000-0000-0000-0000-000000000000",
        device_id=dev, alarm_type="cpu_temp_critical") is None
