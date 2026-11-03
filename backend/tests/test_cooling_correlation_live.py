"""Cooling correlation against the real topology (docs/33 Phase 2).

Skipped unless DCIM_TEST_DATABASE_URL points at a database with an imported
fleet. Every test runs in a transaction that is rolled back, so it can be run
against the live database without leaving an alarm or incident behind.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.alarms import cooling_correlation as cc
from app.alarms import correlation
from app.analytics import correlator

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
            await s.execute(text("UPDATE alarm SET state = 'CLEARED', cleared_at = now() "
                                 "WHERE state <> 'CLEARED'"))
            await s.execute(text("UPDATE anomaly_incident SET state = 'resolved', "
                                 "resolved_at = now() WHERE state = 'open'"))
            cc.reset_graph()
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _hall(s):
    """A hall with CRAHs, rack probes and servers."""
    row = (await s.execute(text("""
        SELECT r.id::text AS room,
               (SELECT d.id::text FROM device d WHERE d.room_id = r.id
                  AND d.device_type = 'crah' ORDER BY d.name LIMIT 1) AS crah,
               (SELECT d.name FROM device d WHERE d.room_id = r.id
                  AND d.device_type = 'crah' ORDER BY d.name LIMIT 1) AS crah_name,
               (SELECT array_agg(d.id::text ORDER BY d.name) FROM device d
                 WHERE d.room_id = r.id AND d.device_type = 'sensor' AND d.rack_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM connection c WHERE c.layer = 'cooling'
                                     AND (c.a_device_id = d.id OR c.b_device_id = d.id)))
                 AS probes,
               (SELECT d.id::text FROM device d WHERE d.room_id = r.id
                  AND d.device_type = 'server' ORDER BY d.name LIMIT 1) AS server
          FROM room r
         WHERE EXISTS (SELECT 1 FROM device d WHERE d.room_id = r.id AND d.device_type = 'crah')
         ORDER BY r.name LIMIT 1
    """))).mappings().first()
    assert row and row["crah"] and row["probes"] and len(row["probes"]) >= 3 and row["server"]
    return row


async def _raise(s, device_id, alarm_type, severity="MAJOR", ago_s=0, category="cooling"):
    t = datetime.now(UTC) - timedelta(seconds=ago_s)
    row = (await s.execute(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, state, message,
                           source, first_seen, last_seen, category)
        VALUES (CAST(:d AS uuid), :t, '', CAST(:sev AS severity_t), 'ACTIVE', 'test',
                'test', :at, :at, :cat)
        RETURNING id::text, severity::text AS severity, first_seen
    """), {"d": device_id, "t": alarm_type, "sev": severity, "at": t,
          "cat": category})).mappings().first()
    return dict(row)


async def _row(s, alarm_id):
    return (await s.execute(text("""
        SELECT is_symptom, root_cause_alarm_id::text AS root, attributes->'l5' AS l5
          FROM alarm WHERE id = CAST(:id AS uuid)"""), {"id": alarm_id})).mappings().first()


async def test_one_stopped_crah_is_one_incident(session):
    s = session
    h = await _hall(s)
    root = await _raise(s, h["crah"], "plant_unit_stopped", "MAJOR", ago_s=240)
    syms = []
    for i, probe in enumerate(h["probes"][:3]):
        a = await _raise(s, probe, "ambient_temp_high", "MAJOR", ago_s=120 - i,
                         category="environmental")
        got = await cc.correlate(s, alarm=a, device_id=probe, alarm_type="ambient_temp_high")
        assert got and got["id"] == root["id"] and got["suppress"]
        syms.append(a["id"])
    hot = await _raise(s, h["server"], "cpu_temp_critical", "CRITICAL", ago_s=60,
                       category="it_equipment")
    got = await cc.correlate(s, alarm=hot, device_id=h["server"],
                             alarm_type="cpu_temp_critical")
    assert got and not got["suppress"]                  # worse than its cause: kept visible

    for i in syms:
        assert (await _row(s, i))["is_symptom"]
    assert not (await _row(s, hot["id"]))["is_symptom"]

    await correlator.refresh_incidents(s)
    inc = await correlator.list_incidents(s, states=["open"], device_id=None, limit=10)
    assert len(inc) == 1 and inc[0]["root_key"] == f"device:{h['crah']}"
    assert inc[0]["tier"] == "unit"
    detail = await correlator.get_incident(s, inc[0]["id"])
    roles = sorted(a["role"] for a in detail["alarms"])
    assert roles == ["folded", "folded", "folded", "member", "root"]
    # The panel asks by device: a member device finds the incident too.
    by_dev = await correlator.list_incidents(s, states=["open"], device_id=h["probes"][0],
                                             limit=10)
    assert [i["id"] for i in by_dev] == [inc[0]["id"]]


async def test_a_root_that_finishes_its_dwell_late_is_adopted_by_the_sweep(session):
    s = session
    h = await _hall(s)
    a = await _raise(s, h["probes"][0], "ambient_temp_high", ago_s=200,
                     category="environmental")
    assert await cc.correlate(s, alarm=a, device_id=h["probes"][0],
                              alarm_type="ambient_temp_high") is None
    root = await _raise(s, h["crah"], "crah_high_temp", ago_s=30)
    out = await cc.sweep(s)
    assert out["adopted"] >= 1
    r = await _row(s, a["id"])
    assert r["is_symptom"] and r["root"] == root["id"]


async def test_symptoms_are_held_while_the_air_recovers_then_released(session):
    s = session
    h = await _hall(s)
    root = await _raise(s, h["crah"], "plant_unit_stopped", ago_s=300)
    a = await _raise(s, h["probes"][0], "ambient_temp_high", ago_s=200,
                     category="environmental")
    await cc.correlate(s, alarm=a, device_id=h["probes"][0], alarm_type="ambient_temp_high")
    # The root clears; the existing release leaves a cooling symptom to the hold.
    await s.execute(text("UPDATE alarm SET state = 'CLEARED', cleared_at = now() "
                         "WHERE id = CAST(:id AS uuid)"), {"id": root["id"]})
    assert await correlation.release_symptoms(s, root["id"]) == []
    await cc.sweep(s)
    assert (await _row(s, a["id"]))["is_symptom"]          # still inside the hold
    await s.execute(text("UPDATE alarm SET cleared_at = now() - make_interval(secs => :h) "
                         "WHERE id = CAST(:id AS uuid)"),
                    {"id": root["id"], "h": cc.RESTORE_HOLD_S + 60})
    out = await cc.sweep(s)
    assert out["released_open"] == 1
    r = await _row(s, a["id"])
    assert not r["is_symptom"] and r["l5"] is None
    await correlator.refresh_incidents(s)
    assert await correlator.list_incidents(s, states=["open"], device_id=None, limit=10) == []


async def test_heat_with_no_cooling_alarm_is_a_room_incident(session):
    s = session
    h = await _hall(s)
    for i, probe in enumerate(h["probes"][:3]):
        await _raise(s, probe, "ambient_temp_high", ago_s=100 + i, category="environmental")
    await correlator.refresh_incidents(s)
    inc = await correlator.list_incidents(s, states=["open"], device_id=None, limit=10)
    assert len(inc) == 1 and inc[0]["tier"] == "room"
    assert inc[0]["root_key"] == f"room:{h['room']}"
    assert "no cooling alarm" in inc[0]["title"]


async def test_a_fire_in_the_hall_stops_the_crah_without_making_it_a_root(session):
    s = session
    h = await _hall(s)
    await _raise(s, h["probes"][-1], "smoke_alert", "CRITICAL", ago_s=400, category="fire")
    await _raise(s, h["crah"], "plant_unit_stopped", ago_s=300)
    a = await _raise(s, h["probes"][0], "ambient_temp_high", ago_s=100,
                     category="environmental")
    assert await cc.correlate(s, alarm=a, device_id=h["probes"][0],
                              alarm_type="ambient_temp_high") is None
