"""Editing alarm rules: what is allowed, and what a bad value would do.

Pure functions, no database, so every check is testable on its own.

The one rule that matters most is the deadband. A numeric rule raises when a
reading crosses the threshold and clears when it comes back past
`clear_threshold`; if the clear point sits on the wrong side of the raise
point the alarm either never clears or clears the moment it raises, and a
reading resting near the limit flaps forever. `check_band` refuses that, and
fills in a sensible clear point when none is given.
"""

from __future__ import annotations

import re
import string
from dataclasses import replace
from typing import Any

from app.alarms.engine import Rule
from app.core.metrics_gen import METRICS

#: Severities a rule may carry. CLEAR is a state, not a severity of a fault.
SEVERITIES = ("INFO", "WARNING", "MINOR", "MAJOR", "CRITICAL")
#: Operators whose threshold and clear point an operator can reason about.
BAND_OPERATORS = (">", ">=", "<", "<=")
RULE_FIELDS = ("threshold", "clear_threshold", "dwell_samples", "dwell_seconds",
               "clear_dwell_samples", "severity", "message_tpl", "enabled")
OVERRIDE_FIELDS = ("threshold", "clear_threshold", "dwell_samples", "dwell_seconds",
                   "clear_dwell_samples", "severity", "enabled", "reason")
#: What a message template may mention. Anything else is a KeyError at raise
#: time, which the engine survives by dropping the message - so refuse it here.
PLACEHOLDERS = frozenset({"value", "threshold", "metric", "alarm_type", "point",
                          "instance", "device", "protocol"})
#: How far a default deadband sits from the raise point, as a share of it.
DEADBAND = 0.05


class RuleEditError(ValueError):
    """The edit is refused. The message is written for the operator."""


def check_template(tpl: str) -> str:
    tpl = (tpl or "").strip()
    if not tpl:
        raise RuleEditError("the message cannot be empty")
    if len(tpl) > 400:
        raise RuleEditError("the message is over 400 characters")
    try:
        names = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
    except ValueError as exc:
        raise RuleEditError(f"the message has unbalanced braces: {exc}") from None
    unknown = sorted(names - PLACEHOLDERS)
    if unknown:
        raise RuleEditError(
            f"the message uses {{{'}, {'.join(unknown)}}}, which a rule cannot fill in; "
            f"use {', '.join('{' + p + '}' for p in sorted(PLACEHOLDERS))}")
    return tpl


def check_range(metric_key: str | None, value: float) -> None:
    """Refuse a threshold the metric can never reach (or that is nonsense)."""
    m = METRICS.get(metric_key or "")
    if m is None:
        return
    lo, hi = getattr(m, "min_valid", None), getattr(m, "max_valid", None)
    if lo is not None and value < lo:
        raise RuleEditError(
            f"{value:g} is below the lowest value {m.display_name} can report ({lo:g} {m.unit})")
    if hi is not None and value > hi:
        raise RuleEditError(
            f"{value:g} is above the highest value {m.display_name} can report ({hi:g} {m.unit})")


def default_clear(operator: str, threshold: float) -> float:
    """A clear point just inside the safe side of the raise point."""
    gap = abs(threshold) * DEADBAND or 1.0
    return threshold - gap if operator in (">", ">=") else threshold + gap


def check_band(operator: str | None, threshold: float | None,
               clear: float | None) -> float | None:
    """Validate the deadband; return the clear point to store."""
    if operator not in BAND_OPERATORS or threshold is None:
        return clear
    if clear is None:
        return default_clear(operator, threshold)
    if operator in (">", ">=") and clear > threshold:
        raise RuleEditError(
            f"the clear point ({clear:g}) is above the raise point ({threshold:g}): "
            f"the alarm would clear the moment it raised. Put it at or below {threshold:g}")
    if operator in ("<", "<=") and clear < threshold:
        raise RuleEditError(
            f"the clear point ({clear:g}) is below the raise point ({threshold:g}): "
            f"the alarm would clear the moment it raised. Put it at or above {threshold:g}")
    return clear


def _int(name: str, v: Any, lo: int, hi: int) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise RuleEditError(f"{name} must be a whole number") from None
    if not lo <= n <= hi:
        raise RuleEditError(f"{name} must be between {lo} and {hi}")
    return n


def _num(name: str, v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise RuleEditError(f"{name} must be a number") from None
    if n != n or n in (float("inf"), float("-inf")):
        raise RuleEditError(f"{name} must be a finite number")
    return n


def validate_fields(rule: dict[str, Any], changes: dict[str, Any],
                    allowed: tuple[str, ...]) -> dict[str, Any]:
    """Check an edit to a rule against the rule it edits. Returns the cleaned
    changes, with a derived clear point added when the threshold moved."""
    unknown = sorted(set(changes) - set(allowed))
    if unknown:
        raise RuleEditError(
            f"{', '.join(unknown)} cannot be edited; editable: {', '.join(allowed)}")
    out: dict[str, Any] = {}
    boolean = (rule.get("metric_kind") or "numeric") == "boolean"
    for k, v in changes.items():
        if k in ("threshold", "clear_threshold"):
            if boolean or rule.get("operator") not in BAND_OPERATORS:
                what = k.replace("_", " ")
                raise RuleEditError(
                    f"{rule['name']} watches a state, not a number: it has no {what}")
            out[k] = _num(k, v)
        elif k == "dwell_samples" or k == "clear_dwell_samples":
            out[k] = _int(k, v, 1, 100)
        elif k == "dwell_seconds":
            out[k] = _int(k, v, 0, 86_400)
        elif k == "severity":
            v = (v or "").upper() or None
            if v is not None and v not in SEVERITIES:
                raise RuleEditError(f"severity must be one of {', '.join(SEVERITIES)}")
            out[k] = v
        elif k == "message_tpl":
            out[k] = check_template(v)
        elif k == "enabled":
            if v is not None and not isinstance(v, bool):
                raise RuleEditError("enabled must be true or false")
            out[k] = v
        elif k == "reason":
            out[k] = (str(v).strip()[:300] or None) if v else None
    return out


def resolve_band(rule: dict[str, Any], out: dict[str, Any],
                 inherited: dict[str, Any] | None = None) -> dict[str, Any]:
    """After `validate_fields`: range-check a moved threshold and make the
    deadband consistent. `inherited` is what the edit falls back to for a
    field it does not set (the rule itself, for an override)."""
    if "threshold" not in out and "clear_threshold" not in out:
        return out
    base = inherited or rule
    thr = out["threshold"] if "threshold" in out else base.get("threshold")
    clr = out["clear_threshold"] if "clear_threshold" in out else base.get("clear_threshold")
    if "threshold" in out and out["threshold"] is not None:
        check_range(rule.get("metric_key"), out["threshold"])
        if "clear_threshold" not in out:
            # The raise point moved and the operator said nothing about the
            # clear point: the old one may now be on the wrong side of it.
            clr = None if _band_broken(rule.get("operator"), thr, clr) else clr
    if "clear_threshold" in out and out["clear_threshold"] is not None:
        check_range(rule.get("metric_key"), out["clear_threshold"])
    if thr is not None:
        out["clear_threshold"] = check_band(rule.get("operator"), thr, clr)
    return out


def _band_broken(op: str | None, thr: float | None, clr: float | None) -> bool:
    if clr is None or thr is None:
        return True
    return (op in (">", ">=") and clr > thr) or (op in ("<", "<=") and clr < thr)


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(name: str) -> str:
    return _SLUG.sub("_", name.lower()).strip("_")[:60]


def validate_new(body: dict[str, Any], device_types: set[str]) -> dict[str, Any]:
    """Validate a new custom threshold rule."""
    name = (body.get("name") or "").strip()
    if not 3 <= len(name) <= 80:
        raise RuleEditError("name the rule in 3 to 80 characters")
    metric = body.get("metric_key")
    if metric not in METRICS:
        raise RuleEditError(f"{metric!r} is not a metric the platform collects")
    op = body.get("operator")
    if op not in BAND_OPERATORS:
        raise RuleEditError(f"operator must be one of {', '.join(BAND_OPERATORS)}")
    thr = _num("threshold", body.get("threshold"))
    if thr is None:
        raise RuleEditError("a threshold rule needs a threshold")
    check_range(metric, thr)
    clr = _num("clear_threshold", body.get("clear_threshold"))
    if clr is not None:
        check_range(metric, clr)
    sev = (body.get("severity") or "").upper()
    if sev not in SEVERITIES:
        raise RuleEditError(f"severity must be one of {', '.join(SEVERITIES)}")
    types = [t for t in (body.get("device_types") or []) if t]
    bad = sorted(set(types) - device_types)
    if bad:
        raise RuleEditError(f"unknown device type: {', '.join(bad)}")
    declared = getattr(METRICS[metric], "device_types", None) or ()
    if types and declared and not set(types) & set(declared):
        raise RuleEditError(
            f"{METRICS[metric].display_name} is only reported by {', '.join(declared)}")
    return {
        "name": name,
        "alarm_type": f"custom_{slug(name)}",
        "metric_key": metric, "operator": op, "threshold": thr,
        "clear_threshold": check_band(op, thr, clr),
        "dwell_samples": _int("dwell_samples", body.get("dwell_samples", 3), 1, 100),
        "dwell_seconds": _int("dwell_seconds", body.get("dwell_seconds"), 0, 86_400),
        "clear_dwell_samples": _int(
            "clear_dwell_samples", body.get("clear_dwell_samples", 2), 1, 100),
        "severity": sev,
        "device_types": types,
        "message_tpl": check_template(
            body.get("message_tpl")
            or f"{METRICS[metric].display_name} {{value}} {op} {{threshold}}"),
    }


# -------------------------------------------------------------- overrides

def apply_override(rule: Rule, ov: dict[str, Any]) -> Rule:
    """The rule as it applies to one device: the override's non-null fields
    over the rule's own. A new object, so the shared rule is never mutated."""
    changes = {k: ov[k] for k in ("threshold", "clear_threshold", "dwell_samples",
                                  "dwell_seconds", "clear_dwell_samples", "severity")
               if ov.get(k) is not None}
    return replace(rule, **changes) if changes else rule


def rules_that_can_fire(rules: list[dict[str, Any]], reported: set[str]) -> list[dict[str, Any]]:
    """The subset of type-matched rules whose metric this device reports.

    Mirrors the engine, which looks rules up by the metric of the sample in
    hand: a rule for a metric the device never sends can never raise, and
    listing it offers an override that changes nothing. An empty `reported`
    means the device has sent nothing yet, and the type-matched list stands,
    because an absence of telemetry is not evidence about what it will send.
    """
    if not reported:
        return rules
    return [r for r in rules if r.get("metric_key") in reported]
