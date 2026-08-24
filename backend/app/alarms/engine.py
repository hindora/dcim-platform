"""Threshold evaluation: dwell, hysteresis and the alarm key.

Pure logic, no database and no I/O, because this is the part that has to be
exercised exhaustively. `CPU > 80 → WARNING` is a threshold, not an alarm
engine: at fleet scale a metric resting on its limit would raise and clear
hundreds of times an hour, and the alarm list becomes something operators stop
reading.

Three things make it usable instead:

* **Dwell** - raise only once the condition has held for N consecutive samples
  (or T seconds), so a single spike is not an alarm.
* **Hysteresis** - clear at a DIFFERENT threshold than the raise. Between the
  two, nothing changes. Omitting that band is what produces flapping, and it is
  the branch most often left out.
* **Alarm key** - (device, alarm_type, instance). An alarm is a stateful object
  under that key, not a row per occurrence, which is what makes raise/update/
  clear idempotent under at-least-once delivery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SEVERITY_RANK = {
    "CLEAR": 0, "INFO": 1, "WARNING": 2, "MINOR": 3, "MAJOR": 4, "CRITICAL": 5,
}


@dataclass(frozen=True, slots=True)
class AlarmKey:
    device_id: str
    alarm_type: str
    instance: str = ""

    def redis_field(self) -> str:
        return f"{self.device_id}|{self.alarm_type}|{self.instance}"


@dataclass(slots=True)
class Rule:
    id: str
    name: str
    alarm_type: str
    severity: str
    message_tpl: str
    metric_key: str | None = None
    operator: str | None = None
    threshold: float | None = None
    clear_threshold: float | None = None
    dwell_samples: int = 3
    dwell_seconds: int | None = None
    clear_dwell_samples: int = 2
    device_types: tuple[str, ...] = ()
    stale_after_s: int | None = None
    enabled: bool = True
    #: Override the classifier for this rule's alarms. Left None unless the
    #: three-layer resolution gets a condition wrong - a rule that has to state
    #: its own category is usually a sign the classifier needs the entry, not
    #: that this rule is special.
    category: str | None = None
    #: How this rule detects: threshold, state, absence, derived, forecast.
    detection: str | None = None
    #: Evaluate only the device-level sample, ignoring per-instance ones.
    #:
    #: Some instances are a BREAKDOWN of the device total rather than separate
    #: things that can fail: an energy monitor publishes its feed and the branch
    #: circuits that add up to it, so one overload raised three alarms. Rack
    #: inlet sensors are the opposite case - each instance is its own reading -
    #: which is why this is per-rule and defaults to off.
    device_total_only: bool = False

    def applies_to(self, device_type: str) -> bool:
        return not self.device_types or device_type in self.device_types

    def applies_to_instance(self, instance: str) -> bool:
        return not (self.device_total_only and instance)


@dataclass(slots=True)
class DwellState:
    """Per (rule, key) progress toward raising or clearing."""

    breach_count: int = 0
    clear_count: int = 0
    first_breach_us: int = 0

    def to_wire(self) -> str:
        return f"{self.breach_count}:{self.clear_count}:{self.first_breach_us}"

    @classmethod
    def from_wire(cls, raw: str | bytes | None) -> DwellState:
        if not raw:
            return cls()
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            b, c, f = raw.split(":")
            return cls(int(b), int(c), int(f))
        except ValueError:
            return cls()


@dataclass(slots=True)
class Candidate:
    """A condition that has satisfied its dwell and should be an alarm."""

    key: AlarmKey
    severity: str
    message: str
    source: str
    observed_at: datetime
    rule_id: str | None = None
    metric_key: str | None = None
    value: float | None = None
    threshold: float | None = None
    endpoint_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClearSignal:
    key: AlarmKey
    observed_at: datetime
    by: str = "system"
    # A clear may resolve a whole family: a PDU dropping under threshold ends
    # both the high and the critical load alarm.
    also_clears: tuple[str, ...] = ()


def compare(value: float, operator: str, threshold: float) -> bool:
    match operator:
        case ">":
            return value > threshold
        case ">=":
            return value >= threshold
        case "<":
            return value < threshold
        case "<=":
            return value <= threshold
        case "==":
            return value == threshold
        case "!=":
            return value != threshold
        case _:
            return False


def _clear_operator(operator: str) -> str:
    """The comparison that decides a value is back inside the safe band."""
    return {">": "<", ">=": "<", "<": ">", "<=": ">"}.get(operator, operator)


def evaluate(
    rule: Rule,
    key: AlarmKey,
    value: float,
    observed_at: datetime,
    state: DwellState,
    endpoint_id: str | None = None,
) -> tuple[Candidate | ClearSignal | None, DwellState]:
    """Advance one rule for one sample.

    Returns the action to take (or None) together with the updated dwell state,
    which the caller persists. Deliberately returns a NEW state rather than
    mutating, so a failed write cannot leave half-applied progress.
    """
    if rule.operator is None or rule.threshold is None:
        return None, state

    observed_us = int(observed_at.timestamp() * 1_000_000)
    breached = compare(value, rule.operator, float(rule.threshold))

    clear_at = (float(rule.clear_threshold)
                if rule.clear_threshold is not None else float(rule.threshold))

    if breached:
        first = state.first_breach_us or observed_us
        new = DwellState(breach_count=state.breach_count + 1, clear_count=0,
                         first_breach_us=first)
        held_samples = new.breach_count >= max(rule.dwell_samples, 1)
        held_time = (rule.dwell_seconds is None
                     or (observed_us - first) / 1_000_000 >= rule.dwell_seconds)
        if held_samples and held_time:
            return Candidate(
                key=key,
                severity=rule.severity,
                message=_render(rule, value),
                source="threshold",
                observed_at=observed_at,
                rule_id=rule.id,
                metric_key=rule.metric_key,
                value=value,
                threshold=float(rule.threshold),
                endpoint_id=endpoint_id,
            ), new
        return None, new

    # Not breaching. Is it back inside the safe band, or merely in the deadband?
    recovered = compare(value, _clear_operator(rule.operator), clear_at)
    if not recovered:
        # BETWEEN clear_threshold and threshold: hold whatever state we are in.
        # This branch is the entire point of hysteresis and the one usually
        # missing - without it a metric oscillating around the limit raises and
        # clears on every sample.
        return None, state

    new = DwellState(breach_count=0, clear_count=state.clear_count + 1,
                     first_breach_us=0)
    if new.clear_count >= max(rule.clear_dwell_samples, 1):
        return ClearSignal(key=key, observed_at=observed_at), DwellState()
    return None, new


def _render(rule: Rule, value: float) -> str:
    try:
        return rule.message_tpl.format(
            value=round(value, 2), threshold=rule.threshold,
            metric=rule.metric_key, alarm_type=rule.alarm_type)
    except (KeyError, IndexError, ValueError):
        # A bad template must not stop an alarm being raised.
        return f"{rule.alarm_type}: {rule.metric_key}={round(value, 2)}"


def escalates(previous: str, current: str) -> bool:
    return SEVERITY_RANK.get(current, 0) > SEVERITY_RANK.get(previous, 0)
