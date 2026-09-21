"""Which of a device's readings becomes the device's reading.

A metric arrives once per instance. For anything with sub-metering that is
many samples of one metric in one poll: a branch-circuit monitor sends
`power_draw` for the panel AND for each of its 42 ways, most of which are
spare and sit at 0 W.

`_note_hot` took whichever landed last, so the last spare way decided the
panel's figure. Twelve energy monitors reporting 115 W read as 0 W - on the
connectivity canvas, on the power page, in PUE and in capacity, all of which
read `device_state.power_w` rather than the samples behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.ingest import writer
from app.ingest.worker import IngestWorker

TS = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
DEV = "11111111-1111-1111-1111-111111111111"


class _Cache:
    hot_metrics = frozenset({"power_draw", "inlet_temperature"})


@pytest.fixture()
def note():
    """`_note_hot` off an otherwise uninitialised worker.

    It touches nothing but its arguments and `self.cache`, which is the point
    of testing it directly: the alternative is a Redis stream, a collector and
    a database to prove an if-statement.
    """
    w = IngestWorker.__new__(IngestWorker)
    w.cache = _Cache()
    hot: dict = {}
    frames: dict = {}

    def call(metric: str, value, instance: str = ""):
        w._note_hot(hot, frames, DEV, metric, value, TS, "good", instance)

    return call, hot, frames


def test_the_panel_total_survives_its_own_spare_ways(note):
    """The bug, in the order the collector sends it."""
    call, hot, _ = note
    call("power_draw", 115.0, "")          # the panel
    call("power_draw", 4.0, "Ckt01")
    call("power_draw", 2.0, "Ckt02")
    call("power_draw", 0.0, "Ckt42")       # a spare way, and the last sample
    assert hot[DEV].power_w == 115.0


def test_order_does_not_decide_it(note):
    """The total is not required to arrive first.

    SNMP walks the sub-tree in OID order and there is no rule that says the
    device-level object comes before the per-circuit table.
    """
    call, hot, _ = note
    call("power_draw", 0.0, "Ckt42")
    call("power_draw", 115.0, "")
    call("power_draw", 4.0, "Ckt01")
    assert hot[DEV].power_w == 115.0


def test_a_device_with_no_total_still_reports(note):
    """Sub-instances are not ignored - they are only outranked.

    A server's inlet temperature arrives named for the sensor that took it and
    there is no device-level object behind it. Dropping those would trade one
    wrong number for no number at all.
    """
    call, hot, _ = note
    call("inlet_temperature", 22.5, "Inlet")
    assert hot[DEV].inlet_temp_c == 22.5


def test_the_live_frame_agrees_with_the_column(note):
    """The websocket frame and the hot column are one reading, not two.

    They are written side by side and read side by side - a canvas that polls
    and a canvas that subscribes must not disagree about what a panel is
    drawing.
    """
    call, hot, frames = note
    call("power_draw", 115.0, "")
    call("power_draw", 0.0, "Ckt42")
    assert frames[DEV]["power_draw"]["v"] == 115.0
    assert hot[DEV].metrics["power_draw"]["v"] == 115.0


def test_a_metric_with_no_hot_column_is_left_alone(note):
    """Only hot metrics are carried; the rest live in the samples table."""
    call, hot, _ = note
    call("water_supply_temp", 7.2, "")
    assert hot == {}


def test_one_metrics_total_does_not_lock_another(note):
    """The lock is per metric, not per device.

    A panel that sends a `power_draw` total and per-sensor temperatures must
    still record the temperature.
    """
    call, hot, _ = note
    call("power_draw", 115.0, "")
    call("inlet_temperature", 24.0, "Inlet")
    assert hot[DEV].power_w == 115.0
    assert hot[DEV].inlet_temp_c == 24.0


def test_the_carrier_starts_with_nothing_claimed():
    """A fresh HotUpdate claims no totals - the set is per device per batch."""
    u = writer.HotUpdate(device_id=DEV, last_seen=TS, metrics={})
    assert u.from_total == set()
    other = writer.HotUpdate(device_id=DEV, last_seen=TS, metrics={})
    u.from_total.add("power_draw")
    assert other.from_total == set(), "the set is shared between instances"
