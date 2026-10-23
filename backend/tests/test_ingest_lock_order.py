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
    those endpoints resolve to, and device_state is the row being locked.

    The key is the device the INVENTORY maps the endpoint to: the row is
    reached through the endpoint in SQL, so a message carrying no device id,
    or a stale one, must not move the row out of device order."""
    import inspect
    from types import SimpleNamespace

    from app.ingest.enrich import EndpointContext
    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    assert body.count("key=self._device_order") == 2, (
        "the device_state and alarm passes are not ordered by device")

    cache = SimpleNamespace(endpoints={
        "e1": EndpointContext("e1", "d2", "snmp", "os_agent"),
        "e2": EndpointContext("e2", "d1", "redfish", "bmc"),
        "e3": EndpointContext("e3", "d2", "redfish", "bmc"),
    })
    me = SimpleNamespace(cache=cache)

    def row(ep, msg_dev):
        return (SimpleNamespace(device_id=msg_dev), "ONLINE", {"endpoint_id": ep})

    rows = [row("e1", ""), row("e3", "zz-stale"), row("e2", "d1"), row("e9", "d0")]
    got = [r[2]["endpoint_id"] for r in sorted(
        rows, key=lambda r: IngestWorker._device_order(me, r))]
    # d0 (unknown endpoint, message device), d1, then d2's two endpoints together.
    assert got == ["e9", "e2", "e1", "e3"]


def test_the_sweep_locks_every_stale_endpoint_in_one_global_order():
    """Two stale collectors whose endpoint ids interleave were locked one
    collector at a time - sorted within each, not across them - against an
    ingest batch that takes the same rows in one sorted pass."""
    import inspect

    from app.alarms import collector_visibility

    body = inspect.getsource(collector_visibility.sweep)
    lock = body.index("FOR UPDATE")
    assert body.index("every = sorted(") < lock < body.index("for owner, endpoint_ids in sorted")


def test_the_alarm_rollup_inserts_in_device_order():
    """The device_state roll-up comes out of a GROUP BY's hash; without an
    ORDER BY its row locks are taken in no order however :ids is sorted."""
    import inspect

    from app.repositories import alarms

    sql = inspect.getsource(alarms.refresh_device_alarm_state)
    assert sql.index("ORDER BY device_id") < sql.index("ON CONFLICT (device_id)")


def test_alarm_work_runs_in_key_order_and_time_order_within_a_key():
    """Dwell needs each key's samples in time order and nothing more; locks
    need one order across keys. Time order across devices gave two workers
    opposite orders over the alarm table's unique index."""
    from datetime import UTC, datetime, timedelta

    from app.alarms.engine import AlarmKey

    t0 = datetime(2026, 10, 23, tzinfo=UTC)
    work = [
        (None, AlarmKey("d2", "temp_high", ""), {"observed_at": t0}),
        (None, AlarmKey("d1", "temp_high", ""), {"observed_at": t0 + timedelta(seconds=30)}),
        (None, AlarmKey("d2", "temp_high", ""), {"observed_at": t0 - timedelta(seconds=30)}),
        (None, AlarmKey("d1", "temp_high", ""), {"observed_at": t0 - timedelta(seconds=60)}),
    ]
    import inspect

    from app.alarms import service
    src = inspect.getsource(service.AlarmService.evaluate_samples)
    assert "work.sort(key=lambda item: (str(item[1].device_id)" in src
    work.sort(key=lambda item: (str(item[1].device_id), item[1].alarm_type,
                                item[1].instance or "", item[2]["observed_at"]))
    order = [(k.device_id, s["observed_at"]) for _, k, s in work]
    assert order == sorted(order)


def test_the_endpoint_parameters_carry_only_what_the_statement_binds():
    """The sort key comes off the message, not the parameter dict, so the
    dict handed to the endpoint_state upsert gains no stray column."""
    import inspect

    from app.ingest.worker import IngestWorker

    body = inspect.getsource(IngestWorker._handle_endpoint_state)
    assert '"device_id": st.device_id' not in body
