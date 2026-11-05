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
            # Cleared a day ago, not now: a cause cleared inside the hold still
            # explains new heat, and the live board's real alarms must not.
            await s.execute(text("UPDATE alarm SET state = 'CLEARED', "
                                 "cleared_at = now() - interval '1 day' "
                                 "WHERE state <> 'CLEARED' "
                                 "OR cleared_at > now() - interval '1 hour'"))
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
    b = await _raise(s, h["probes"][1], "ambient_temp_high", ago_s=190,
                     category="environmental")
    for x, probe in ((a, h["probes"][0]), (b, h["probes"][1])):
        await cc.correlate(s, alarm=x, device_id=probe, alarm_type="ambient_temp_high")
    # The root clears; the existing release leaves a cooling symptom to the hold.
    await s.execute(text("UPDATE alarm SET state = 'CLEARED', cleared_at = now() "
                         "WHERE id = CAST(:id AS uuid)"), {"id": root["id"]})
    assert await correlation.release_symptoms(s, root["id"]) == []
    out = await cc.sweep(s)
    # Inside the hold, with its cause gone: one of the kind speaks for the
    # room, the other stays folded. The room still says it is hot.
    assert out["spoken"] == 1
    seen = [(await _row(s, x["id"]))["is_symptom"] for x in (a, b)]
    assert sorted(seen) == [False, True]
    await s.execute(text("UPDATE alarm SET cleared_at = now() - make_interval(secs => :h) "
                         "WHERE id = CAST(:id AS uuid)"),
                    {"id": root["id"], "h": cc.RESTORE_HOLD_S + 60})
    out = await cc.sweep(s)
    assert out["released_open"] == 2
    for x in (a, b):
        r = await _row(s, x["id"])
        assert not r["is_symptom"] and r["l5"] is None
    await correlator.refresh_incidents(s)
    assert await correlator.list_incidents(s, states=["open"], device_id=None, limit=10) == []


async def test_heat_with_no_cooling_alarm_is_a_room_incident(session):
    s = session
    h = await _hall(s)
    for i, probe in enumerate(h["probes"][:3]):
        await _raise(s, probe, "ambient_temp_high", ago_s=100 + i, category="environmental")
    # Too soon: a cooling root may still be finishing its dwell.
    await correlator.refresh_incidents(s)
    assert await correlator.list_incidents(s, states=["open"], device_id=None, limit=10) == []
    await s.execute(text("UPDATE alarm SET first_seen = first_seen - interval '15 minutes' "
                         "WHERE state <> 'CLEARED' AND alarm_type = 'ambient_temp_high'"))
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


async def test_a_hall_losing_its_crahs_reads_as_one_incident(session):
    """hall-crahs, 2026-11-04: seven CRAHs down, 90 servers at critical inlet.

    Before the fix that was 7 + 90 lines. Now: one incident, the other units
    as peers, ONE visible critical per kind, the rest folded.
    """
    s = session
    h = await _hall(s)
    crahs = (await s.execute(text("""
        SELECT id::text FROM device WHERE room_id = CAST(:r AS uuid) AND device_type = 'crah'
         ORDER BY name LIMIT 2"""), {"r": h["room"]})).scalars().all()
    servers = (await s.execute(text("""
        SELECT id::text FROM device WHERE room_id = CAST(:r AS uuid) AND device_type = 'server'
         ORDER BY name LIMIT 3"""), {"r": h["room"]})).scalars().all()
    first = await _raise(s, crahs[0], "plant_unit_stopped", ago_s=300)
    await _raise(s, crahs[1], "plant_unit_stopped", ago_s=290)
    hot = []
    for i, srv in enumerate(servers):
        a = await _raise(s, srv, "inlet_temp_critical", "CRITICAL", ago_s=200 - i,
                         category="environmental")
        got = await cc.correlate(s, alarm={**a, "metric_key": "inlet_temperature"},
                                 device_id=srv, alarm_type="inlet_temp_critical")
        assert got and got["id"] == first["id"]
        hot.append(a["id"])
    assert not (await _row(s, hot[0]))["is_symptom"]          # the one that speaks
    assert (await _row(s, hot[1]))["is_symptom"] and (await _row(s, hot[2]))["is_symptom"]

    await correlator.refresh_incidents(s)
    inc = await correlator.list_incidents(s, states=["open"], device_id=None, limit=10)
    assert len(inc) == 1 and "1 more cooling unit" in inc[0]["title"]
    roles = sorted(a["role"] for a in (await correlator.get_incident(s, inc[0]["id"]))["alarms"])
    assert roles == ["folded", "folded", "member", "peer", "root"]


async def test_a_plant_failing_in_sequence_is_one_incident(session):
    """all-chillers, 2026-11-04: CHL1 stopped and its alarm cleared as the
    sequencer called CHL2, which was held off, then CHL3. Rooted per chiller,
    that was three incidents, and CHL1's symptoms would have been released
    onto the console when its alarm cleared."""
    s = session
    h = await _hall(s)
    chillers = (await s.execute(text("""
        SELECT d.id::text FROM device d JOIN room r ON r.id = d.room_id
         WHERE d.device_type = 'chiller'
           AND r.datacenter_id = (SELECT datacenter_id FROM room WHERE id = CAST(:r AS uuid))
         ORDER BY d.name LIMIT 2"""), {"r": h["room"]})).scalars().all()
    first = await _raise(s, chillers[0], "plant_unit_stopped", ago_s=400)
    a = await _raise(s, h["probes"][0], "ambient_temp_high", ago_s=300, category="environmental")
    got = await cc.correlate(s, alarm=a, device_id=h["probes"][0], alarm_type="ambient_temp_high")
    assert got and got["id"] == first["id"]
    await correlator.refresh_incidents(s)
    first_inc = await correlator.list_incidents(s, states=["open"], device_id=None, limit=10)
    assert len(first_inc) == 1 and first_inc[0]["root_key"] == f"device:{chillers[0]}"
    # The sequencer moves on: CHL1's alarm clears, CHL2's opens.
    await s.execute(text("UPDATE alarm SET state = 'CLEARED', cleared_at = now() "
                         "WHERE id = CAST(:id AS uuid)"), {"id": first["id"]})
    second = await _raise(s, chillers[1], "plant_unit_stopped", ago_s=200)
    b = await _raise(s, h["probes"][1], "ambient_temp_high", ago_s=150, category="environmental")
    assert (await cc.correlate(s, alarm=b, device_id=h["probes"][1],
                               alarm_type="ambient_temp_high"))["id"] == second["id"]

    out = await cc.sweep(s)
    assert out["moved"] == 1                         # a's heat is CHL2's now, not released
    r = await _row(s, a["id"])
    assert r["is_symptom"] and r["root"] == second["id"]

    await correlator.refresh_incidents(s)
    inc = await correlator.list_incidents(s, states=["open"], device_id=None, limit=10)
    assert len(inc) == 1 and inc[0]["tier"] == "plant"
    # The lead's incident, keyed on CHL1, keeps its identity now that the root
    # is CHL2's: one event, one id, one common alarm (2026-11-05).
    assert inc[0]["id"] == first_inc[0]["id"]            # the same incident, not a new one
    assert inc[0]["root_key"] == f"device:{chillers[0]}"
    assert inc[0]["root_alarm_id"] == second["id"]
    common = (await s.execute(text("""
        SELECT count(*) FROM alarm WHERE alarm_type = 'cascade_incident' AND state <> 'CLEARED'
    """))).scalar()
    assert common <= 1
