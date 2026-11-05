"""Dew point, derived where a device reports temperature and humidity together.

ASHRAE bounds moisture by dew point and no probe here reports one, so the
ingest worker derives it (migration 0132). The number has to be the one a
psychrometric chart gives, and it must never be invented from half a pair.
"""

from __future__ import annotations

import pytest

from app.ingest.worker import DEW_POINT_METRIC, dew_point_c


class _Ctx:
    def __init__(self, device_type):
        self.device_type = device_type
        self.rated_power_w = None


class _Row:
    def __init__(self, metric_id, value, device_id="dev-1", instance="", quality="good"):
        self.metric_id = metric_id
        self.value = value
        self.device_id = device_id
        self.instance = instance
        self.ts = "2026-11-05T00:00:00Z"
        self.quality = quality


IDS = {"ambient_temperature": 1, "relative_humidity": 2, DEW_POINT_METRIC: 3}


def _worker(device_type="sensor"):
    from app.ingest.worker import IngestWorker

    w = IngestWorker.__new__(IngestWorker)

    class Cache:
        def __init__(self):
            self.devices = {"dev-1": _Ctx(device_type)}

        def metric_id(self, key):
            return IDS.get(key)

    w.cache = Cache()
    w._note_hot = lambda *a, **k: None
    return w


@pytest.mark.parametrize("t, rh, expected", [
    (24.0, 55.0, 14.5),    # a hall inside the RH limits, already near the 15 C line
    (22.0, 45.0, 9.5),
    (30.0, 70.0, 24.0),
    (18.0, 20.0, -5.1),
])
def test_the_magnus_formula_matches_the_chart(t, rh, expected):
    assert dew_point_c(t, rh) == pytest.approx(expected, abs=0.3)


def test_a_pair_in_one_poll_yields_one_dew_point():
    w = _worker()
    rows = [_Row(1, 24.0), _Row(2, 55.0)]
    inputs: list = []
    w._derive_dew_point(rows, inputs, {}, [])
    assert len(rows) == 3 and rows[2].metric_id == 3
    assert rows[2].value == pytest.approx(14.5, abs=0.3)
    assert inputs[0]["metric"] == DEW_POINT_METRIC


def test_half_a_pair_yields_nothing():
    w = _worker()
    for rows in ([_Row(1, 24.0)], [_Row(2, 55.0)]):
        inputs: list = []
        w._derive_dew_point(rows, inputs, {}, [])
        assert len(rows) == 1 and inputs == []


def test_a_probe_with_no_hygrometer_is_not_a_dry_room():
    """Plant instruments report RH 0: there is no sensor behind it, not dry air."""
    w = _worker()
    rows = [_Row(1, 7.0), _Row(2, 0.0)]
    inputs: list = []
    w._derive_dew_point(rows, inputs, {}, [])
    assert len(rows) == 2 and inputs == []


def test_pairs_are_matched_by_instance():
    """A PDU with two probes is two dew points, each from its own pair."""
    w = _worker("pdu")
    rows = [_Row(1, 24.0, instance="Probe 1"), _Row(2, 55.0, instance="Probe 1"),
            _Row(1, 30.0, instance="Probe 2"), _Row(2, 70.0, instance="Probe 2")]
    inputs: list = []
    w._derive_dew_point(rows, inputs, {}, [])
    got = {r.instance: r.value for r in rows if r.metric_id == 3}
    assert got["Probe 1"] == pytest.approx(14.5, abs=0.3)
    assert got["Probe 2"] == pytest.approx(24.0, abs=0.3)


def test_a_device_that_reports_its_own_dew_point_is_left_alone():
    w = _worker()
    rows = [_Row(1, 24.0), _Row(2, 55.0), _Row(3, 14.2)]
    inputs: list = []
    w._derive_dew_point(rows, inputs, {}, [])
    assert len(rows) == 3


def test_a_bad_reading_does_not_feed_the_rules():
    """A BAD half (the instrument's own fault flag) taints the pair: stored
    for the record, never judged."""
    w = _worker()
    rows = [_Row(1, 24.0, quality="bad"), _Row(2, 55.0)]
    inputs: list = []
    w._derive_dew_point(rows, inputs, {}, [])
    assert len(rows) == 3 and inputs == []
