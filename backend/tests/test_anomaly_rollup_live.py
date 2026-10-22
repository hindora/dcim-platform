"""device_state.max_severity follows a derived raise and its clear.

raise_alarm does not refresh the device rollup; the sweep calls
refresh_device_alarm_state for every device it touched (docs/29 D12). Racks,
rooms, topology and the device list read device_state, so a derived alert
that skipped this would be invisible everywhere but the alarm list.

Skipped unless DCIM_TEST_DATABASE_URL is set. Rolled back.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.analytics import sweep
from tests.test_anomaly_rules_live import add_samples, make_crah
from tests.test_anomaly_rules_live import session as session

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


async def _rollup(s, dev):
    return (await s.execute(text("""
        SELECT max_severity::text AS sev, health::text AS health, active_alarms
          FROM device_state WHERE device_id = CAST(:d AS uuid)"""), {"d": dev})
    ).mappings().first()


async def test_max_severity_follows_raise_and_clear(session):
    now = datetime.now(UTC).replace(microsecond=0)
    dev = await make_crah(session, now, supply=25.0)
    await sweep.run_sweep(session, now)
    r = await _rollup(session, dev)
    assert r["sev"] == "MINOR" and r["health"] == "WARNING" and r["active_alarms"] >= 1

    later = now + timedelta(seconds=120)
    await add_samples(session, dev, now + timedelta(seconds=30), later, supply=20.5)
    await sweep.run_sweep(session, later)
    r = await _rollup(session, dev)
    assert r["sev"] == "CLEAR" and r["health"] == "OK" and r["active_alarms"] == 0
