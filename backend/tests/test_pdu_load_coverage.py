"""How a PDU overload gets noticed.

It had exactly one way: an APC trap. `power_load_high` fires on `load_pct` and
lists pdu and rpp among its device types, but a rack PDU never published that
metric - it reports current, power, voltage and energy, and load_pct arrived
only from the UPS, generator, switchgear, MCC and MPP. The rule was armed
against two device types it could never fire on.

A trap is one unacknowledged UDP datagram. On this platform they were lost for
three hours when a simulator restart reset the receiver port and every
notification went to a closed socket. An overloading PDU would have been silent
for all of it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

MAPPING = (pathlib.Path(__file__).resolve().parents[2]
           / "contracts" / "mappings" / "snmp" / "traps.yaml")

APC_LOAD_VARBIND = "1.3.6.1.4.1.318.1.1.12.2.3.1.1.2"   # rPDULoadStatusLoad


def blocks() -> list[str]:
    return MAPPING.read_text(encoding="utf-8").split("\n  - oid: ")[1:]


def apc_load_blocks() -> list[str]:
    return [b for b in blocks()
            if re.search(r"event_type: (pdu_load_|breaker_tripped|outlet_current)", b)
            and "[apc]" in b]


# ------------------------------------------------------- the trap's reading

def test_apc_load_traps_carry_the_reading_the_pdu_sent():
    """The alarm used to say "Load High" and nothing else.

    The number was on the wire the whole time - PowerNet defines these
    notifications to carry rPDULoadStatusLoad, and the simulator sends it.
    """
    found = apc_load_blocks()
    assert found, "no APC load traps in the mapping at all"
    for b in found:
        assert APC_LOAD_VARBIND in b, (
            f"{b.splitlines()[0]} declares no reading")


def test_the_reading_is_scaled_out_of_vendor_units():
    """rPDULoadStatusLoad is TENTHS of an amp.

    Published raw under a metric measured in amps, 135 becomes "135 A" on a
    13.5 A circuit: plausible, wrong by a factor of ten, and indistinguishable
    from the overload it is meant to report.
    """
    for b in apc_load_blocks():
        assert "value_scale: 0.1" in b, (
            f"{b.splitlines()[0]} publishes tenths of an amp as amps")


def test_no_limit_is_invented_where_the_vendor_sends_none():
    """PowerNet carries the STATE it crossed, not the number.

    An empty threshold is the honest answer; a fabricated one is how "0 C,
    limit 0 C" reached an operator earlier in this platform's life.
    """
    for b in apc_load_blocks():
        assert "threshold_varbind:" not in b, (
            f"{b.splitlines()[0]} claims a limit APC does not send")


def test_every_scaled_entry_actually_has_something_to_scale():
    text = MAPPING.read_text(encoding="utf-8")
    for b in blocks():
        if "value_scale:" in b:
            assert "value_varbind:" in b, (
                f"{b.splitlines()[0]} scales a reading it does not take")
    assert "value_scale:" in text


# ------------------------------------------------ the polled backstop

def test_load_pct_is_derived_for_the_device_types_the_rule_targets():
    """The rule and the data have to agree on who reports what.

    `power_load_high` lists pdu and rpp; those are exactly the types that
    report watts without reporting what fraction of themselves that is.
    """
    from app.ingest.worker import LOAD_PCT_FROM_DRAW

    assert "pdu" in LOAD_PCT_FROM_DRAW
    assert "rpp" in LOAD_PCT_FROM_DRAW


def test_nothing_is_derived_over_a_reading_the_device_already_takes():
    """The UPS, generator, switchgear, MCC and MPP publish load_pct directly.

    Deriving on top of them would put two different answers to one question in
    the same series, and the derived one would win or lose by arrival order.
    """
    from app.ingest.worker import LOAD_PCT_FROM_DRAW

    for t in ("ups", "generator", "switchgear", "mcc", "mpp"):
        assert t not in LOAD_PCT_FROM_DRAW, (
            f"{t} reports load_pct itself; deriving it again would collide")


class _Ctx:
    def __init__(self, device_type, rated):
        self.device_type = device_type
        self.rated_power_w = rated


class _Row:
    def __init__(self, metric_id, value):
        self.metric_id = metric_id
        self.value = value
        self.device_id = "dev-1"
        self.instance = ""
        self.ts = "2026-08-31T00:00:00Z"
        self.quality = "good"


def _worker(device_type="pdu", rated=4992.0, metrics=None):
    from app.ingest.worker import IngestWorker

    w = IngestWorker.__new__(IngestWorker)
    ids = metrics if metrics is not None else {"power_draw": 1, "load_pct": 2}

    class Cache:
        def __init__(self):
            self.devices = {"dev-1": _Ctx(device_type, rated)}

        def metric_id(self, key):
            return ids.get(key)

    w.cache = Cache()
    w._note_hot = lambda *a, **k: None
    return w


@pytest.mark.parametrize("draw,rated,expected", [
    (4492.8, 4992.0, 90.0),
    (2496.0, 4992.0, 50.0),
    (4992.0, 4992.0, 100.0),
])
def test_the_derivation_is_draw_over_nameplate(draw, rated, expected):
    w = _worker(rated=rated)
    rows, inputs = [_Row(1, draw)], []
    w._derive_load_pct(rows, inputs, {}, [])

    assert len(rows) == 2
    assert rows[1].value == pytest.approx(expected)
    assert inputs[0]["metric"] == "load_pct"
    assert inputs[0]["value"] == pytest.approx(expected)


def test_a_pdu_with_no_nameplate_is_skipped_not_guessed():
    """"Percent of a number we guessed" is worse than no percentage.

    It would read as a measurement, and an operator would act on it.
    """
    w = _worker(rated=None)
    rows, inputs = [_Row(1, 4000.0)], []
    w._derive_load_pct(rows, inputs, {}, [])
    assert len(rows) == 1 and inputs == []


def test_a_device_type_that_reports_its_own_load_is_untouched():
    w = _worker(device_type="ups")
    rows, inputs = [_Row(1, 4000.0)], []
    w._derive_load_pct(rows, inputs, {}, [])
    assert len(rows) == 1 and inputs == []


def test_it_does_nothing_if_the_metric_is_not_registered():
    w = _worker(metrics={"power_draw": 1})
    rows, inputs = [_Row(1, 4000.0)], []
    w._derive_load_pct(rows, inputs, {}, [])
    assert len(rows) == 1 and inputs == []
