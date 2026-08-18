"""Counter -> rate derivation.

This is where NMS bugs live, so the rules are explicit and each one exists
because the alternative produces a visible defect:

* A missing baseline emits **nothing**, not zero. Zero is a value and it lies.
* An explicit ``counter_reset`` (agent restart seen via sysUpTime, or a gNMI
  stream reconnect) discards the delta rather than reporting a spike.
* A backwards counter is a wrap if the implied delta is under half the counter
  width, and a reset otherwise. A 64-bit counter that "wrapped" by 9 quintillion
  did not wrap.
* A gap longer than ``max_gap_s`` is discarded: an average over six hours is not
  a data point, it is a line that ruins the chart's scale.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.metrics_gen import METRICS, RATE_TARGETS


@dataclass(frozen=True, slots=True)
class Baseline:
    """The previous counter reading for one (endpoint, metric, instance)."""

    value: int
    observed_at_us: int


@dataclass(frozen=True, slots=True)
class RateResult:
    metric: str
    value: float
    observed_at_us: int


class DiscardReason:
    NO_BASELINE = "no_baseline"
    EXPLICIT_RESET = "explicit_reset"
    NON_POSITIVE_DT = "non_positive_dt"
    GAP_TOO_LONG = "gap_too_long"
    IMPLAUSIBLE = "implausible_backwards"
    NOT_A_COUNTER = "not_a_counter"
    NO_RATE_TARGET = "no_rate_target"


def baseline_key(endpoint_id: str, metric: str, instance: str) -> str:
    return f"{endpoint_id}|{metric}|{instance}"


def derive(
    *,
    metric: str,
    instance: str,
    current: int,
    observed_at_us: int,
    counter_bits: int,
    counter_reset: bool,
    baseline: Baseline | None,
    max_gap_s: int = 900,
) -> tuple[RateResult | None, str | None]:
    """Return ``(rate, None)`` or ``(None, discard_reason)``.

    The caller stores the new baseline regardless of the outcome - a discarded
    sample still advances the reference point.
    """
    definition = METRICS.get(metric)
    if definition is None or definition.value_type != "counter":
        return None, DiscardReason.NOT_A_COUNTER

    target = RATE_TARGETS.get(metric)
    if target is None:
        return None, DiscardReason.NO_RATE_TARGET

    if baseline is None:
        return None, DiscardReason.NO_BASELINE
    if counter_reset:
        return None, DiscardReason.EXPLICIT_RESET

    dt_s = (observed_at_us - baseline.observed_at_us) / 1_000_000
    if dt_s <= 0:
        return None, DiscardReason.NON_POSITIVE_DT
    if dt_s > max_gap_s:
        return None, DiscardReason.GAP_TOO_LONG

    delta = current - baseline.value
    if delta < 0:
        width = 1 << (counter_bits or 64)
        wrapped = delta + width
        # A genuine wrap is a small delta once corrected. More than half the
        # counter width means the agent reset and sysUpTime did not tell us.
        if wrapped > width // 2:
            return None, DiscardReason.IMPLAUSIBLE
        delta = wrapped

    scale = METRICS[target].rate_scale or 1.0
    return RateResult(metric=target, value=(delta / dt_s) * scale,
                      observed_at_us=observed_at_us), None


def in_valid_range(metric: str, value: float) -> bool:
    """Registry range check. Out-of-range is SUSPECT quality, not a drop."""
    d = METRICS.get(metric)
    if d is None:
        return False
    if d.min_valid is not None and value < d.min_valid:
        return False
    return not (d.max_valid is not None and value > d.max_valid)
