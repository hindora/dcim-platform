"""The real sweep against a real database (docs/33 task 1.11).

One CRAH, discharging 5 K over setpoint with its valve wide open: the sweep
reads its telemetry, CR-1 opens, and ONE alarm is raised with
source='analytics', detection='derived', severity MINOR and
attributes.anomaly.finding_id pointing at the finding - with the outbox row
written in the same transaction (an integration that opted the type in).
Then the condition clears: the finding resolves `cleared`, KEEPING a verdict
an operator gave it (docs/29 D3), and the alarm clears.

Skipped unless DCIM_TEST_DATABASE_URL is set (needs 0112). The transaction is
rolled back; Redis is the sweep's in-process store, never a real server.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.analytics import sweep
from app.analytics import window as win_store
from app.integrations import outbox as integration_outbox
from app.repositories import anomalies as repo

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    win_store.reset_memory()
    async with maker() as s:
        trans = await s.begin()
        try:
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()
            win_store.reset_memory()


async def make_crah(session, now: datetime, *, supply: float) -> str:
    dev = await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle)
        VALUES (:n, 'crah', 'in_service') RETURNING id::text
    """), {"n": f"CRAH-T-{uuid.uuid4().hex[:6]}"})
    # Dwell 0 on this device only: the dwell framework is unit-tested; here
    # the subject is the database path.
    await session.execute(text("""
        INSERT INTO anomaly_override (detector_id, device_id, params, created_by)
        VALUES ('CR-1', CAST(:d AS uuid),
                '{"dwell_s": 0, "clear_dwell_s": 0}'::jsonb, 'test')
    """), {"d": dev})
    await add_samples(session, dev, now - timedelta(seconds=1500), now, supply=supply)
    for k in range(3):
        await session.execute(text("""
            INSERT INTO telemetry_bool (ts, device_id, metric_id, instance, value, quality)
            SELECT :ts, CAST(:d AS uuid), id, 'Unit_Running', true, 'good'
              FROM metric WHERE key = 'equipment_state'
        """), {"ts": now - timedelta(seconds=1700 - 900 * k), "d": dev})
    return dev


async def add_samples(session, dev, start, end, *, supply):
    t, k = start, 0
    while t <= end:
        for key, inst, v in (("supply_air_temp", "", supply + 0.1 * (k % 3)),
                             ("air_setpoint_temp", "", 20.0),
                             ("valve_position_pct", "CHW", 100.0),
                             ("return_air_temp", "", 33.0 + 0.1 * (k % 2)),
                             ("fan_speed_pct", "", 60.0 + (k % 2))):
            await session.execute(text("""
                INSERT INTO telemetry_sample (ts, device_id, metric_id, instance, value, quality)
                SELECT :ts, CAST(:d AS uuid), id, :inst, :v, 'good' FROM metric WHERE key = :k
            """), {"ts": t, "d": dev, "inst": inst, "v": v, "k": key})
        t += timedelta(seconds=30)
        k += 1


async def test_one_derived_alarm_with_its_finding_and_outbox_row(session):
    now = datetime.now(UTC).replace(microsecond=0)
    dev = await make_crah(session, now, supply=25.0)
    integ = await session.scalar(text("""
        INSERT INTO integration (kind, name, enabled, base_url, config, secret_enc, secret_kind)
        VALUES ('jira_cloud', :n, true, 'https://example.invalid',
                CAST(:c AS jsonb), '\\x00'::bytea, 'api_token') RETURNING id::text
    """), {"n": f"t-{uuid.uuid4().hex[:6]}",
           "c": json.dumps({"policy": {"alarm_types": ["crah_supply_over_setpoint"],
                                       "dwell_s": 0}})})

    r = await sweep.run_sweep(session, now)
    assert r.ran and r.alerts_raised >= 1
    written = await integration_outbox.enqueue_actions(session, r.actions)

    f = (await session.execute(text("""
        SELECT id::text, alarm_id::text, lane, kind, state, layer
          FROM anomaly_finding WHERE device_id = CAST(:d AS uuid) AND detector_id = 'CR-1'
    """), {"d": dev})).mappings().one()
    assert f["lane"] == "alert" and f["kind"] == "process" and f["layer"] == 2
    alarms = (await session.execute(text("""
        SELECT id::text, source, detection, severity::text AS severity, attributes,
               metric_key, alarm_type, category
          FROM alarm WHERE device_id = CAST(:d AS uuid)
           AND alarm_type = 'crah_supply_over_setpoint'
    """), {"d": dev})).mappings().all()
    assert len(alarms) == 1
    a = alarms[0]
    assert a["source"] == "analytics" and a["detection"] == "derived"
    assert a["severity"] == "MINOR" and a["metric_key"] == "supply_air_temp"
    assert a["category"] == "cooling"
    assert a["attributes"]["anomaly"]["finding_id"] == f["id"]
    assert a["attributes"]["anomaly"]["detector_id"] == "CR-1"
    assert f["alarm_id"] == a["id"]
    # The outbox row exists in THIS transaction, beside the alarm.
    assert written >= 1
    n = await session.scalar(text("""
        SELECT count(*) FROM integration_outbox
         WHERE integration_id = CAST(:i AS uuid) AND alarm_id = CAST(:a AS uuid)
    """), {"i": integ, "a": a["id"]})
    assert n == 1
    # GET /findings?alarm_id= finds it.
    page = await repo.list_findings(session, alarm_id=a["id"], with_total=True)
    assert page["total"] == 1 and page["items"][0]["severity"] == "MINOR"
    assert page["items"][0]["layer"] == "L2"
    detail = await repo.get_finding(session, f["id"])
    assert detail["evidence"]["rule"]["detector_id"] == "CR-1"
    assert detail["evidence"]["diagnosis"] == "chw_starved"
    assert [s["id"] for s in detail["evidence"]["signals"]][:2] == ["S1", "S2"]
    assert detail["feedback"] == []
    facets = (await repo.list_findings(session, device_id=dev, with_facets=True))["facets"]
    assert facets["by_lane"]["alert"] >= 1 and facets["open_findings"] >= 1
    # Frames for the opening came back with the section 4.1 row.
    opened = [x for x in r.frames if x["event"] == "anomaly.finding.opened"
              and x["finding"]["id"] == f["id"]]
    assert opened and opened[0]["finding"]["device_id"] == dev


async def test_clearing_resolves_keeps_the_verdict_and_clears_the_alarm(session):
    now = datetime.now(UTC).replace(microsecond=0)
    dev = await make_crah(session, now, supply=25.0)
    await sweep.run_sweep(session, now)
    fid = await session.scalar(text("""
        SELECT id::text FROM anomaly_finding WHERE device_id = CAST(:d AS uuid)
           AND detector_id = 'CR-1' AND state = 'open'"""), {"d": dev})
    done = await repo.apply_feedback(session, fid, action="dismiss", actor="op",
                                     reason="expected_operation", note=None)
    assert done["to_state"] == "dismissed"

    later = now + timedelta(seconds=120)
    await add_samples(session, dev, now + timedelta(seconds=30), later, supply=20.5)
    r = await sweep.run_sweep(session, later)
    assert r.findings_resolved >= 1
    row = (await session.execute(text("""
        SELECT state, verdict, resolution, resolved_by FROM anomaly_finding
         WHERE id = CAST(:f AS uuid)"""), {"f": fid})).mappings().one()
    assert dict(row) == {"state": "resolved", "verdict": "dismissed",
                         "resolution": "cleared", "resolved_by": "system"}
    st = await session.scalar(text("""
        SELECT state::text FROM alarm WHERE device_id = CAST(:d AS uuid)
           AND alarm_type = 'crah_supply_over_setpoint'"""), {"d": dev})
    assert st == "CLEARED"


async def test_a_reopen_inside_the_window_reuses_the_row(session):
    now = datetime.now(UTC).replace(microsecond=0)
    dev = await make_crah(session, now, supply=25.0)
    await sweep.run_sweep(session, now)
    t1 = now + timedelta(seconds=90)
    await add_samples(session, dev, now + timedelta(seconds=30), t1, supply=20.5)
    await sweep.run_sweep(session, t1)
    t2 = t1 + timedelta(seconds=90)
    await add_samples(session, dev, t1 + timedelta(seconds=30), t2, supply=26.0)
    await sweep.run_sweep(session, t2)
    rows = (await session.execute(text("""
        SELECT state, reopen_count, jsonb_array_length(evidence -> 'alarm_history') AS h
          FROM anomaly_finding WHERE device_id = CAST(:d AS uuid) AND detector_id = 'CR-1'
    """), {"d": dev})).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "open" and rows[0]["reopen_count"] == 1
    assert rows[0]["h"] == 1          # the first, cleared alarm is history
