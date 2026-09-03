"""The nightly snapshot: the rules that keep a trend trustworthy.

The behaviour - idempotent take, series shape, activity classification - is
proved against a real database in the scratch run. Guarded here is what a later
change would quietly break: the idempotence key, the two-source rule, and the
worker actually being wired to take the thing.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
SNAPSHOTS = (APP / "repositories" / "snapshots.py").read_text(encoding="utf-8")
WORKER = (APP / "ingest" / "worker.py").read_text(encoding="utf-8")


def test_taking_a_snapshot_is_idempotent_on_the_day():
    """Two workers, a restart mid-tick, a manual run - one row.

    The day is the primary key and the insert defers to it. A snapshot that
    could land twice would show a day disagreeing with itself.
    """
    body = SNAPSHOTS[SNAPSHOTS.index("async def take"):]
    body = body[:body.index("async def series")]
    assert "ON CONFLICT (day) DO NOTHING" in body


def test_the_snapshot_is_one_statement():
    """One consistent read. A snapshot assembled from several queries could
    count a device that moved between them twice, or not at all."""
    body = SNAPSHOTS[SNAPSHOTS.index("async def take"):]
    body = body[:body.index("async def series")]
    assert body.count("session.execute") == 1


def test_activity_comes_from_events_not_snapshot_diffs():
    """Ten installs and ten decommissions in one day net to zero in a diff.

    Counts of state are snapshots; movements between states are events, and
    the activity query must read the event table.
    """
    body = SNAPSHOTS[SNAPSHOTS.index("async def lifecycle_activity"):]
    assert "device_lifecycle_event" in body
    assert "asset_snapshot" not in body


def test_a_maintenance_round_trip_is_not_an_install():
    """maintenance -> in_service is a machine coming BACK, not arriving.

    Counting it would inflate installs every time planned work ended, and the
    chart would report churn as growth.
    """
    body = SNAPSHOTS[SNAPSHOTS.index("async def lifecycle_activity"):]
    assert re.search(r"from_state NOT IN \('in_service',\s*'installed',\s*"
                     r"'maintenance'\)", body)


def test_the_worker_takes_the_snapshot():
    """A snapshot nobody takes is a trend that never starts. The check runs on
    the worker's own tick and starts at -inf, so a worker that was down at
    midnight records today on its first tick rather than leaving a hole."""
    tree = ast.parse(WORKER)
    tick = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_tick")
    called = {ast.unparse(n.func) for n in ast.walk(tick)
              if isinstance(n, ast.Call)}
    assert "self._maybe_snapshot" in called
    assert 'self._last_snapshot_check = float("-inf")' in WORKER


def test_a_failed_snapshot_does_not_stop_ingest():
    """Missing one check is a snapshot an hour late, absorbed by the day key.
    Stopping telemetry ingestion would not be."""
    body = WORKER[WORKER.index("async def _maybe_snapshot"):]
    body = body[:body.index("async def _maybe_advance_maintenance")]
    assert "except Exception" in body
    assert "log.error" in body
