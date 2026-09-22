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


def test_endpoint_state_finishes_one_table_before_starting_the_other():
    """The inversion sorting cannot fix.

    Writing both tables per endpoint interleaves them - es, ds, es, ds - so
    this handler can hold a device_state row while waiting for an
    endpoint_state row that the telemetry handler already holds, and the
    telemetry handler is waiting for that device_state row. Neither batch is
    out of order; the interleave alone is enough.
    """
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    first = body.index("upsert_endpoint_state(session")
    second = body.index("apply_device_status(session")
    assert first < second, (
        "device_state is written before endpoint_state is finished, which "
        "interleaves the two tables against _handle_telemetry's order")
    # And the two must be separate passes, not two calls in one loop.
    assert body.count("for ") >= 3, "the passes have been merged back together"


def test_the_device_pass_is_ordered_by_device_not_endpoint():
    """A device has several endpoints - a server has a BMC and an OS agent -
    so endpoint order says nothing about the order of the device_state rows
    those endpoints resolve to, and device_state is the row being locked."""
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    tail = body[body.index("apply_device_status(session") - 400:
                body.index("apply_device_status(session")]
    assert "device_id" in tail, (
        "the device_state pass is not ordered by device id")


def test_the_endpoint_parameters_carry_only_what_the_statement_binds():
    """The sort key comes off the message, not the parameter dict, so the
    dict handed to the endpoint_state upsert gains no stray column."""
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    assert '"device_id": st.device_id' not in body
