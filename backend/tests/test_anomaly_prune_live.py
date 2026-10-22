"""The nightly batched prune deletes only rows past retention (docs/29 6.3).

The prune commits per batch (that is the point: no long transaction on the
tight host), so this test cannot roll back. It works on its own device and
detector rows and deletes them at the end; run it against a throwaway
database only.

Skipped unless DCIM_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.analytics import baselines

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)


async def _finding(conn, dev, mid, *, state, resolved_ago_days=None, inst=""):
    resolved = NOW - timedelta(days=resolved_ago_days) if resolved_ago_days else None
    return await conn.scalar(text("""
        INSERT INTO anomaly_finding (detector_id, detector_version, layer, lane, device_id,
                                     metric_id, instance, first_seen, raised_at, last_seen,
                                     summary, state, resolved_at, resolution, verdict)
        VALUES ('DQ-1', 1, 0, 'advisory', CAST(:d AS uuid), :m, :i, :f, :f, :f, 's',
                :st, :r, CASE WHEN :st = 'resolved' THEN 'cleared' END, NULL)
        RETURNING id::text
    """), {"d": dev, "m": mid, "i": inst, "st": state, "r": resolved,
           "f": (resolved or NOW) - timedelta(hours=1)})


async def test_only_rows_past_retention_go_and_in_batches():
    engine = create_async_engine(DB_URL, poolclass=None)
    try:
        async with engine.connect() as conn:
            dev = await conn.scalar(text("""
                INSERT INTO device (name, device_type, lifecycle)
                VALUES (:n, 'sensor', 'in_service') RETURNING id::text
            """), {"n": f"SEN-P-{uuid.uuid4().hex[:6]}"})
            mid = await conn.scalar(text("SELECT id FROM metric WHERE key = 'inlet_temperature'"))
            old = [await _finding(conn, dev, mid, state="resolved", resolved_ago_days=800,
                                  inst=f"o{i}") for i in range(7)]
            keep_resolved = await _finding(conn, dev, mid, state="resolved",
                                           resolved_ago_days=300, inst="k1")
            keep_open = await _finding(conn, dev, mid, state="open", inst="k2")
            await conn.execute(text("""
                INSERT INTO anomaly_state (detector_id, device_id, metric_id, instance,
                                           detector_version, state, updated_at)
                VALUES ('DQ-1', CAST(:d AS uuid), :m, 'stale', 1, '{}'::jsonb, :old),
                       ('DQ-1', CAST(:d AS uuid), :m, 'fresh', 1, '{}'::jsonb, :new)
            """), {"d": dev, "m": mid, "old": NOW - timedelta(days=8),
                   "new": NOW - timedelta(days=1)})
            await conn.execute(text("""
                INSERT INTO anomaly_override (detector_id, device_id, enabled, reason,
                                              expires_at, created_by)
                VALUES ('DQ-1', CAST(:d AS uuid), false, 'x', :exp, 't')
            """), {"d": dev, "exp": NOW - timedelta(days=10)})
            await conn.execute(text("""
                INSERT INTO anomaly_baseline_exclusion (device_id, starts_at, ends_at, source,
                                                        reason, created_by)
                VALUES (CAST(:d AS uuid), :s1, :e1, 'manual', 'old', 't'),
                       (CAST(:d AS uuid), :s2, :e2, 'manual', 'recent', 't')
            """), {"d": dev, "s1": NOW - timedelta(days=900), "e1": NOW - timedelta(days=890),
                   "s2": NOW - timedelta(days=30), "e2": NOW - timedelta(days=20)})
            await conn.commit()

            out = await baselines.prune(conn, NOW, batch=3)   # 7 old rows -> 3 batches
            assert out["anomaly_finding"] == 7
            assert out["anomaly_state"] == 1 and out["anomaly_override"] == 1
            assert out["anomaly_baseline_exclusion"] == 1

            left = set((await conn.execute(text("""
                SELECT id::text FROM anomaly_finding WHERE device_id = CAST(:d AS uuid)
            """), {"d": dev})).scalars().all())
            assert left == {keep_resolved, keep_open} and not (left & set(old))
            states = (await conn.execute(text("""
                SELECT instance FROM anomaly_state WHERE device_id = CAST(:d AS uuid)
            """), {"d": dev})).scalars().all()
            assert states == ["fresh"]
            # A second pass has nothing to do.
            again = await baselines.prune(conn, NOW, batch=3)
            assert again["anomaly_finding"] == 0

            await conn.execute(text("DELETE FROM device WHERE id = CAST(:d AS uuid)"),
                               {"d": dev})
            await conn.commit()
    finally:
        async with engine.connect() as conn:      # never leave rows behind
            await conn.execute(text("DELETE FROM device WHERE name LIKE 'SEN-P-%'"))
            await conn.commit()
        await engine.dispose()
