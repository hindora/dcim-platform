"""The deterministic record of why a finding opened (docs/29 section 7.4).

Every item carries an id (S1, S2, ...) that Phase 5's explainer must cite;
nothing here is generated text beyond fixed templates. The evidence is built
when a finding opens and rewritten only on a state change, so a TOASTed
jsonb is not rewritten every 60 s (docs/29 section 4.7 HOT discipline).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.analytics.detectors.rules import DeviceView, Verdict, finite, slope_per_s

EVIDENCE_VERSION = 1


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def _round(v: float | None, nd: int = 3) -> float | None:
    v = finite(v)
    return None if v is None else round(v, nd)


def signal_summary(dev: DeviceView, key: tuple[str, str], since: float) -> dict[str, Any]:
    """min/max/mean/slope/last/n of one series over the evidence window."""
    out: dict[str, Any] = {"device_id": dev.device_id, "metric": key[0], "instance": key[1]}
    if key in dev.bools:
        b = dev.bools[key]
        out.update({"kind": "state", "last": None if b.value is None else int(b.value),
                    "since": _iso(b.since) if b.since else None,
                    "since_is_transition": b.since_known,
                    "starts": [_iso(t) for t in b.starts if t >= since]})
        return out
    s = dev.clean(key, since=since)
    if not s:
        out.update({"n": 0})
        return out
    vals = [x.v for x in s]
    slope = slope_per_s(s)
    out.update({"min": _round(min(vals)), "max": _round(max(vals)),
                "mean": _round(sum(vals) / len(vals)),
                "slope_per_h": _round(slope * 3600 if slope is not None else None),
                "last": _round(vals[-1]), "n": len(vals)})
    return out


def rule_evidence(*, detector_id: str, detector_version: int, params: dict[str, Any],
                  verdict: Verdict, dev: DeviceView, now: float,
                  first_seen: float, window_s: float = 1800) -> dict[str, Any]:
    """The evidence jsonb for an L0 or L2 finding."""
    since = min(first_seen, now - window_s)
    signals = [{"id": f"S{i}", **signal_summary(dev, k, since)}
               for i, k in enumerate(verdict.signals, start=1)]
    upper = lower = None
    if verdict.threshold is not None:
        # Rules whose condition is "below" a floor carry the floor as `lower`.
        if verdict.diagnosis in ("low_delta_t", "condensation_risk") \
                or verdict.metric_key == "basin_level_pct":
            lower = _round(verdict.threshold)
        else:
            upper = _round(verdict.threshold)
    return {
        "v": EVIDENCE_VERSION,
        "window": {"from": _iso(since), "to": _iso(now)},
        "rule": {"detector_id": detector_id, "expr": verdict.expr,
                 "dwell_s": params.get("dwell_s"),
                 "dwell_samples": params.get("dwell_samples"),
                 "clear_dwell_s": params.get("clear_dwell_s"),
                 "params_version": detector_version},
        "diagnosis": verdict.diagnosis,
        "expected": _round(verdict.expected), "observed": _round(verdict.observed),
        "lower": lower, "upper": upper,
        "signals": signals,
        "gates": {"maintenance_window": dev.maintenance,
                  "fire_predischarge": dev.fire_predischarge,
                  "online": dev.online},
        "masks": [{"series": f"{dev.device_id}|{k[0]}|{k[1]}", "masked": k in dev.masked}
                  for k in verdict.signals if k in dev.num],
        "baseline": None,
        "alarm_history": [],
    }
