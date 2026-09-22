"""Deadlock avoidance in the ingest write path.

Two ingest workers is the supported configuration - one cannot keep up - so
every multi-row write they share is a chance for them to take each other's row
locks in opposite orders. Postgres resolves that by killing a tick outright,
which loses a round of telemetry and reads in the log as a database fault
rather than an ordering bug.

Two invariants close it, and both are invisible at a glance, which is why they
are pinned here:

  1. Within a statement, rows are ordered by their key.
  2. Across handlers, the TABLES are locked in one order estate-wide:
     endpoint_state before device_state.

Neither has an observable effect in a single-worker test run, so nothing else
in the suite would notice if either were undone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.ingest import writer

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


class Recorder:
    """Captures the parameter lists handed to `execute`, in order."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return None

    def sql_containing(self, needle):
        return [p for sql, p in self.calls if needle in sql]


def hot(device_id, **over):
    row = {"device_id": device_id, "last_seen": NOW, "metrics": {"a": 1},
           "power_w": None, "inlet_temp_c": None, "cpu_util_pct": None,
           "humidity_pct": None}
    row.update(over)
    return writer.HotUpdate(**row)


# ------------------------------------------------- ordering within a batch

async def test_device_state_is_written_in_key_order():
    """The batch arrives in message order, which differs per worker."""
    rec = Recorder()
    ids = [str(uuid.UUID(int=i)) for i in (5, 1, 9, 3)]
    await writer.upsert_device_state(rec, [hot(i) for i in ids])
    sent = rec.sql_containing("INSERT INTO device_state")[0]
    assert [p["device_id"] for p in sent] == sorted(ids)


async def test_device_state_ordering_does_not_drop_or_duplicate_rows():
    """Sorting must reorder the batch, not reshape it."""
    rec = Recorder()
    ids = [str(uuid.UUID(int=i)) for i in (4, 2, 7)]
    written = await writer.upsert_device_state(rec, [hot(i) for i in ids])
    sent = rec.sql_containing("INSERT INTO device_state")[0]
    assert written == 3
    assert {p["device_id"] for p in sent} == set(ids)


async def test_endpoint_telemetry_is_written_in_key_order():
    rec = Recorder()
    seen = {"e-9": NOW, "e-2": NOW + timedelta(seconds=1), "e-5": NOW}
    await writer.touch_endpoint_telemetry(rec, seen)
    sent = rec.sql_containing("last_telemetry_at")[0]
    assert [p["id"] for p in sent] == ["e-2", "e-5", "e-9"]
    # The timestamp must still travel with its own endpoint.
    assert {p["id"]: p["ts"] for p in sent} == seen


# --------------------------------------------- ordering across the handlers

def test_telemetry_locks_endpoint_state_before_device_state():
    """`_handle_endpoint_state` reaches device_state THROUGH an endpoint, so
    its order is fixed by its own shape. The telemetry path is the one with a
    free choice, and it has to make the same one."""
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_telemetry)
    touch = body.index("touch_endpoint_telemetry(session")
    upsert = body.index("upsert_device_state(session")
    assert touch < upsert, (
        "device_state is locked before endpoint_state in _handle_telemetry, "
        "inverting _handle_endpoint_state's order - the two handlers will "
        "deadlock under two workers")


def test_endpoint_state_payloads_are_processed_in_key_order():
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    assert "sorted(payloads" in body, (
        "endpoint payloads are walked in arrival order, so two workers can "
        "take the same rows in opposite orders")
