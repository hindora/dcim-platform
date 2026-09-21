"""Reading a panel schedule off a meter, and what it is worth once read.

A branch-circuit monitor stores which breaker each CT is clamped to. Until
this platform read it, every branch on every meter was an anonymous number and
a transfer switch metered at 102 kW on channel 1 had to be reported as the sum
of the loads underneath it instead - an inference, published beside
measurements, and a different number.
"""

from __future__ import annotations

import pytest

from app.schemas import TopologyEdge, TopologyNode
from app.services.meter_commissioning import channels_of
from app.services.meter_schedule import _decode_charstring, parse_label
from app.services.topology import _derive_power

# ───────────────────────────────────────────── what a description means

@pytest.mark.parametrize("desc,expected", [
    ("Circuit 1 Active Power — ATS1-DC1-UR", "ATS1-DC1-UR"),
    ("Circuit 42 Current — PDUA-DC1-HA-R2-01", "PDUA-DC1-HA-R2-01"),
    # A hand-commissioned meter was typed by a person, who had a hyphen key.
    ("Circuit 7 Active Power - RPPA-DC1-CP", "RPPA-DC1-CP"),
])
def test_a_commissioned_channel_yields_its_branch(desc, expected):
    assert parse_label(desc) == expected


@pytest.mark.parametrize("desc", [
    "Circuit 3 Active Power — Spare",
    "Circuit 3 Active Power — spare",
    "Circuit 3 Active Power — Unused",
])
def test_a_spare_way_is_not_a_branch(desc):
    assert parse_label(desc) is None


def test_an_uncommissioned_meter_yields_nothing():
    """The factory description names no branch, and none may be invented.

    A meter nobody has commissioned is a real state of a real site. Reading a
    branch into it would attribute load on the strength of a channel number.
    """
    assert parse_label("Circuit 1 Active Power") is None
    assert parse_label("Panel Total kW") is None


def test_the_charstring_comes_out_of_an_ack():
    """Scanned for by tag, not read from a fixed offset.

    The ACK's header length varies with how the object identifier encodes, and
    a fixed offset would read a label one byte short - silently, and only for
    some object instances.
    """
    text = "Circuit 1 Active Power — ATS1-DC1-UR".encode()
    frame = bytes([0x30, 0x05, 0x0C]) + bytes([0x75, len(text) + 1, 0x00]) + text
    assert _decode_charstring(frame) == "Circuit 1 Active Power — ATS1-DC1-UR"


def test_a_frame_with_no_string_is_not_a_label():
    assert _decode_charstring(bytes([0x30, 0x05, 0x0C, 0x44, 0x42, 0xC8, 0x00, 0x00])) is None


# ───────────────────────────────────────────── how many channels to ask about

def test_the_channel_count_comes_off_the_model():
    assert channels_of("Verdigris EV2-84") == 84
    assert channels_of("Verdigris EV2-24") == 24


def test_an_unknown_model_is_not_assumed_to_be_the_biggest():
    """Every channel over the real count is a timeout, and the import waits."""
    assert channels_of("") == 42
    assert channels_of("Some Other Meter") == 42


# ───────────────────────────────────────────── what the reading is worth

def node(nid, name, dtype, power=None, rolled=0):
    metrics = {} if power is None else {"power_w": power}
    return TopologyNode(id=nid, name=name, device_type=dtype,
                        metrics=metrics, rolled_up=rolled)


def edge(src, dst):
    return TopologyEdge(id=f"{src}->{dst}", source=src, target=dst, layer="power")


CHANNEL = {"ats": {"power_w": 102335.0, "instance": "Ckt01",
                   "meter_name": "EV21-DC1-UR"}}


def test_a_measured_channel_is_what_the_switch_reports():
    ats = node("ats", "ATS1-DC1-UR", "ats")
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    mcc = node("mcc", "MCC1-DC1-MR", "mcc", 35000.0)
    _derive_power([ats, ups, mcc],
                  [edge("ats", "ups"), edge("ats", "mcc")], CHANNEL)

    assert ats.derived_power_w == 102335.0
    assert ats.derived_power_kind == "channel"
    assert ats.derived_power_from == "EV21-DC1-UR Ckt01"


def test_the_instrument_outranks_the_inference():
    """The sum is 110 kW and the CT says 102.3 kW; the CT is on the conductor.

    They differ for a real reason - the sum is of what this platform has a
    record of, and a switch carries whatever is actually corded to it - which
    is exactly why the measurement wins.
    """
    ats = node("ats", "ATS1-DC1-UR", "ats")
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    mcc = node("mcc", "MCC1-DC1-MR", "mcc", 35000.0)
    _derive_power([ats, ups, mcc],
                  [edge("ats", "ups"), edge("ats", "mcc")], CHANNEL)
    assert ats.derived_power_w == 102335.0     # not 110000.0


def test_without_a_schedule_the_sum_still_answers():
    """The import is an improvement, not a dependency.

    An estate whose meters were never commissioned keeps the behaviour it had
    rather than losing the figure it used to show.
    """
    ats = node("ats", "ATS1-DC1-UR", "ats")
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    mcc = node("mcc", "MCC1-DC1-MR", "mcc", 35000.0)
    _derive_power([ats, ups, mcc], [edge("ats", "ups"), edge("ats", "mcc")], {})
    assert ats.derived_power_w == 110000.0
    assert ats.derived_power_kind == "downstream"


def test_a_channel_with_no_recent_reading_falls_through():
    """A stale channel is not a reading. The window is enforced in the query;
    this is the shape that arrives when nothing came back."""
    ats = node("ats", "ATS1-DC1-UR", "ats")
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    _derive_power([ats, ups], [edge("ats", "ups")],
                  {"ats": {"power_w": None, "instance": "Ckt01",
                           "meter_name": "EV21-DC1-UR"}})
    assert ats.derived_power_w == 75000.0
    assert ats.derived_power_kind == "downstream"


def test_a_device_that_meters_itself_ignores_the_channel():
    """Its own telemetry is closer to it than any external CT."""
    ups = node("ups", "UPSA-DC1-UR", "ups", 75000.0)
    _derive_power([ups], [], {"ups": {"power_w": 1.0, "instance": "Ckt09",
                                      "meter_name": "EV21-DC1-UR"}})
    assert ups.derived_power_w is None
    assert ups.metrics["power_w"] == 75000.0
