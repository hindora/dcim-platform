"""Migration 0111's tables behave as docs/29 section 14 says.

Constraint names exactly as written (no double prefix), the finding dedupe
key, the policy CHECKs (no MAJOR default, the L5 switch row), one detector
per alarm_type, one current baseline per series, the RESTRICT on a detector
with findings, and no hypertables.

Skipped unless DCIM_TEST_DATABASE_URL is set. Every statement runs inside a
rolled-back transaction; expected failures run inside savepoints.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]

TABLES = ("anomaly_detector", "anomaly_override", "anomaly_baseline_exclusion",
          "anomaly_baseline", "anomaly_state", "anomaly_incident", "anomaly_finding")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        trans = await s.begin()
        # These are 0111's schema checks, written against empty tables; 0112
        # seeds the catalogue (CR-1, L5, ...). Empty it inside the rolled-back
        # transaction so the tests insert their own rows as before.
        await s.execute(text("DELETE FROM anomaly_finding"))
        await s.execute(text("DELETE FROM anomaly_detector"))
        try:
            yield s
        finally:
            await trans.rollback()
            await engine.dispose()


async def _refused(session, sql, params=None):
    with pytest.raises((IntegrityError, DBAPIError)):
        async with session.begin_nested():
            await session.execute(text(sql), params or {})


async def _detector(session, did="CR-1", **kw):
    row = {"id": did, "layer": 2, "phase": 1, "lane": "alert",
           "alarm_type": f"t_{uuid.uuid4().hex[:6]}", "severity": "MINOR",
           "types": ["crah"]}
    row.update(kw)
    await session.execute(text("""
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, default_lane, alarm_type, severity)
        VALUES (:id, :layer, :phase, 'n', 'd', CAST(:types AS text[]), 'supply_air_temp',
                :lane, :alarm_type, CAST(:severity AS severity_t))
    """), row)


async def _device(session) -> str:
    return await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle)
        VALUES (:n, 'crah', 'in_service') RETURNING id::text
    """), {"n": f"crah-{uuid.uuid4().hex[:8]}"})


async def _metric(session) -> int:
    return await session.scalar(text("SELECT id FROM metric ORDER BY id LIMIT 1"))


_FINDING = """
    INSERT INTO anomaly_finding (detector_id, detector_version, layer, lane, device_id,
                                 metric_id, first_seen, raised_at, last_seen, summary,
                                 state, resolved_at, resolution)
    VALUES (:det, 1, 2, 'alert', CAST(:dev AS uuid), :met, now(), now(), now(), 's',
            :state, CASE WHEN :state = 'resolved' THEN now() END,
            CASE WHEN :state = 'resolved' THEN 'cleared' END)
"""


async def test_seven_plain_tables_and_constraint_names_as_written(session):
    found = (await session.execute(text("""
        SELECT relname FROM pg_class WHERE relkind = 'r' AND relname LIKE 'anomaly\\_%'
    """))).scalars().all()
    assert sorted(found) == sorted(TABLES)
    hyper = await session.scalar(text("""
        SELECT count(*) FROM timescaledb_information.hypertables
         WHERE hypertable_name LIKE 'anomaly%'"""))
    assert hyper == 0
    names = (await session.execute(text("""
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'anomaly_finding'::regclass AND contype = 'c'"""))).scalars().all()
    assert "ck_anomaly_finding_verdict_state" in names
    assert all(n.startswith("ck_anomaly_finding_") and "ck_ck_" not in n for n in names)
    fk = await session.scalar(text("""
        SELECT count(*) FROM pg_constraint
         WHERE conname = 'fk_anomaly_incident_root_finding_id_anomaly_finding'"""))
    assert fk == 1


async def test_detector_policy_checks(session):
    await _refused(session, """
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, default_lane, alarm_type, severity)
        VALUES ('CR-9', 2, 1, 'n', 'd', '{crah}', 'x', 'alert', 'x', 'MAJOR')""")
    await _refused(session, """
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, default_lane)
        VALUES ('CR-9', 2, 1, 'n', 'd', '{crah}', 'x', 'alert')""")
    await _refused(session, """
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric)
        VALUES ('XX-1', 5, 2, 'n', 'd', '{}', '*')""")
    # The L5 switch row itself is accepted.
    await session.execute(text("""
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, params)
        VALUES ('L5', 5, 2, 'correlator', 'switch', '{}', '*',
                '{"major_enabled": false}'::jsonb)"""))
    await _refused(session, """
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, cadence_s)
        VALUES ('CR-8', 2, 1, 'n', 'd', '{crah}', 'x', 90)""")


async def test_two_detectors_cannot_share_an_alarm_type(session):
    await _detector(session, "CR-1", alarm_type="crah_supply_over_setpoint")
    await _refused(session, """
        INSERT INTO anomaly_detector (id, layer, phase, name, diagnosis, device_types,
                                      primary_metric, default_lane, alarm_type, severity)
        VALUES ('CR-2', 2, 1, 'n', 'd', '{crah}', 'x', 'alert',
                'crah_supply_over_setpoint', 'MINOR')""")


async def test_one_open_finding_per_series_and_restrict_on_the_detector(session):
    await _detector(session, "CR-1")
    dev, met = await _device(session), await _metric(session)
    p = {"det": "CR-1", "dev": dev, "met": met}
    await session.execute(text(_FINDING), {**p, "state": "open"})
    await _refused(session, _FINDING, {**p, "state": "open"})
    await session.execute(text("UPDATE anomaly_finding SET state = 'resolved', "
                               "resolved_at = now(), resolution = 'cleared'"))
    await session.execute(text(_FINDING), {**p, "state": "open"})  # key free again
    await _refused(session, """
        UPDATE anomaly_finding SET state = 'dismissed' WHERE state = 'open'""")
    await _refused(session, "DELETE FROM anomaly_detector WHERE id = 'CR-1'")
    # Deleting the device cascades its findings.
    await session.execute(text("DELETE FROM device WHERE id = CAST(:d AS uuid)"), {"d": dev})
    assert await session.scalar(text("SELECT count(*) FROM anomaly_finding")) == 0


async def test_one_current_baseline_per_series(session):
    await _detector(session, "CH-2", layer=3, phase=3, lane="advisory", alarm_type=None,
                    severity=None, types=["chiller"])
    dev, met = await _device(session), await _metric(session)
    ins = """
        INSERT INTO anomaly_baseline (detector_id, detector_version, device_id, metric_id,
                                      model_kind, model, source_rollup, fit_from, fit_to, n,
                                      status, is_current)
        VALUES ('CH-2', 1, CAST(:d AS uuid), :m, 'ols', '{}'::jsonb, 'telemetry_1h',
                now() - interval '28 days', now(), 168, :status, :cur)"""
    await session.execute(text(ins), {"d": dev, "m": met, "status": "ok", "cur": True})
    await _refused(session, ins, {"d": dev, "m": met, "status": "ok", "cur": True})
    await _refused(session, ins, {"d": dev, "m": met, "status": "rejected", "cur": True})
    await session.execute(text(ins), {"d": dev, "m": met, "status": "rejected", "cur": False})
