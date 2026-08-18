"""Counter -> rate derivation.

Every case here corresponds to a real production failure pattern. If one of
these regresses, a chart grows a spike or a gap that somebody spends a day
chasing.
"""

from __future__ import annotations

import pytest

from app.ingest.rates import Baseline, DiscardReason, derive, in_valid_range

SEC = 1_000_000  # microseconds


def _derive(prev, curr, dt_us=SEC, bits=64, reset=False, metric="if_in_octets",
            max_gap_s=900):
    baseline = None if prev is None else Baseline(value=prev, observed_at_us=0)
    return derive(metric=metric, instance="1", current=curr,
                  observed_at_us=dt_us, counter_bits=bits, counter_reset=reset,
                  baseline=baseline, max_gap_s=max_gap_s)


def test_normal_delta_is_scaled_to_bits_per_second():
    rate, reason = _derive(1000, 2000)
    assert reason is None
    # if_in_bps has rate_scale 8: 1000 octets/s is 8000 bits/s.
    assert rate is not None and rate.metric == "if_in_bps"
    assert rate.value == pytest.approx(8000.0)


def test_missing_baseline_emits_nothing_not_zero():
    rate, reason = _derive(None, 2000)
    assert rate is None
    assert reason == DiscardReason.NO_BASELINE


def test_explicit_reset_is_discarded():
    rate, reason = _derive(9000, 100, reset=True)
    assert rate is None
    assert reason == DiscardReason.EXPLICIT_RESET


def test_32_bit_wrap_is_corrected():
    rate, reason = _derive(4_294_967_290, 100, bits=32)
    assert reason is None
    # (2^32 - 4294967290) + 100 == 106 octets
    assert rate is not None and rate.value == pytest.approx(106 * 8)


def test_64_bit_wrap_is_corrected():
    rate, reason = _derive((1 << 64) - 10, 90, bits=64)
    assert reason is None
    assert rate is not None and rate.value == pytest.approx(100 * 8)


def test_implausible_backwards_jump_is_a_reset_not_a_wrap():
    """A 64-bit counter that 'wrapped' by nine quintillion did not wrap."""
    rate, reason = _derive(9_000_000, 5, bits=64)
    assert rate is None
    assert reason == DiscardReason.IMPLAUSIBLE


def test_gap_longer_than_max_is_discarded():
    rate, reason = _derive(1000, 2000, dt_us=6 * 3600 * SEC)
    assert rate is None
    assert reason == DiscardReason.GAP_TOO_LONG


def test_zero_or_negative_dt_is_discarded():
    rate, reason = _derive(1000, 2000, dt_us=0)
    assert rate is None
    assert reason == DiscardReason.NON_POSITIVE_DT


def test_gauge_metric_is_not_a_counter():
    rate, reason = _derive(1000, 2000, metric="cpu_utilization")
    assert rate is None
    assert reason == DiscardReason.NOT_A_COUNTER


def test_counter_without_a_rate_target_emits_nothing():
    rate, reason = _derive(1000, 2000, metric="sys_uptime")
    assert rate is None
    assert reason == DiscardReason.NO_RATE_TARGET


@pytest.mark.parametrize(("metric", "value", "expected"), [
    ("cpu_utilization", 50.0, True),
    ("cpu_utilization", 150.0, False),
    ("cpu_utilization", -1.0, False),
    ("cpu_temperature", 67.5, True),
    ("cpu_temperature", 200.0, False),
    ("not_a_real_metric", 1.0, False),
])
def test_range_validation(metric, value, expected):
    assert in_valid_range(metric, value) is expected
