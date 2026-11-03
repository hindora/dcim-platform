"""L3: a device judged against its own learned normal (docs/35).

The Phase 1 rules (rules.py) know what normal is from engineering - a
setpoint, a design delta-T. These do not: they learn it, per series, from the
device's own history (analytics/baselines.py fits it nightly) and ask whether
what it is doing now is unlike what it usually does at this hour.

Scoring, once per sweep for a series with a fitted baseline:

  x       the mean of the last `score_window_s` of clean samples - the same
          kind of figure the baseline was fitted from (hourly means), so a
          single noisy poll cannot trip it
  z       (x - expected) / sigma, expected and sigma for this UTC hour
  EWMA    e = a*z + (1-a)*e', updated only on a sweep with a new sample:
          a sharp change crosses `z_open` within a few polls
  CUSUM   S+ = max(0, S+ + z - k), S- = max(0, S- - z - k): a slow drift
          (a fraction of sigma per hour) accumulates until it crosses `h`

and the verdict goes through the same dwell framework as every other detector
(rules.step), so a finding needs `dwell_s` to open and `clear_dwell_s` clean
to close.

HELD (cond None), never false, when it cannot judge: no baseline, the baseline
still learning, this hour not learned, too few samples, the first
`coldstart_s` after the series came back from a gap (every morning on a
part-time estate), or the series masked by L0. A held series neither opens nor
clears - a sensor gone quiet must not resolve a finding by itself.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.analytics.detectors.rules import Key, RuleInput, Verdict, context_gate

#: A gap this long before the window means the series has just come back.
GAP_S = 3600.0


def _held(key: Key, why: str, **kw: Any) -> Verdict:
    """Cannot judge. `x` stays None so rules.step keeps the EWMA/CUSUM state as
    it was: a held sweep must neither advance nor erase it."""
    return Verdict(key[0], key[1], None, diagnosis=why, **kw)


def hour_entry(model: Mapping[str, Any], hour: int, min_samples: int
               ) -> tuple[float, float, int] | None:
    """(expected, sigma, n) for one UTC hour, or None if the hour is not learned."""
    got = (model.get("hod") or {}).get(str(hour))
    if not got or int(got[2]) < min_samples:
        return None
    return float(got[0]), float(got[1]), int(got[2])


def step_stats(prev: Mapping[str, Any] | None, z: float, *, alpha: float, k: float
               ) -> dict[str, float]:
    """One EWMA and two-sided CUSUM update. Pure; the state lives in DwellState.x."""
    p = prev or {}
    e = z if "e" not in p else alpha * z + (1 - alpha) * float(p["e"])
    sp = max(0.0, float(p.get("sp", 0.0)) + z - k)
    sn = max(0.0, float(p.get("sn", 0.0)) - z - k)
    return {"e": round(e, 4), "sp": round(sp, 4), "sn": round(sn, 4)}


def tripped(st: Mapping[str, float], *, z_open: float, h: float, direction: str) -> bool:
    high = st["e"] > z_open or st["sp"] > h
    low = st["e"] < -z_open or st["sn"] > h
    return high if direction == "high" else low if direction == "low" else high or low


def score_profile(ri: RuleInput, metric: str, *, unit: str, what: str,
                  direction: str = "both", instance: str | None = None) -> list[Verdict]:
    """Every instance of `metric` on the device against its hour-of-day profile."""
    dev = ri.dev
    keys = [dev.pick(metric, instance)] if instance is not None else (
        dev.instances(metric) or [dev.pick(metric)])
    return [_score_one(ri, key, unit=unit, what=what, direction=direction) for key in keys]


def _score_one(ri: RuleInput, key: Key, *, unit: str, what: str, direction: str) -> Verdict:
    p, dev, now = ri.params, ri.dev, ri.now
    kw = {"signals": (key,), "fresh": key in dev.fresh}
    gate = context_gate(p, dev)
    if gate:
        return Verdict(key[0], key[1], False, gate=gate, **kw)
    if not dev.online:
        return Verdict(key[0], key[1], False, gate="offline", **kw)
    if key in dev.masked:
        return _held(key, "masked", **kw)
    base = ri.baseline(key)
    if base is None:
        return _held(key, "no_baseline", **kw)
    if base.get("status") != "ok":
        return _held(key, "learning", **kw)

    window = float(p.get("score_window_s", 1800))
    all_clean = dev.clean(key)
    recent = [s for s in all_clean if s.ts >= now - window]
    if len(recent) < int(p.get("min_samples", 3)):
        return _held(key, "too_few_samples", **kw)
    # Back from a gap: the first stretch after the series resumes is a cold
    # start (collectors reconnecting, walks resuming), not behaviour.
    start = _resumed_at(all_clean, now)
    if start is not None and now - start < float(p.get("coldstart_s", 1800)):
        return _held(key, "cold_start", **kw)

    hour = datetime.fromtimestamp(now, UTC).hour
    entry = hour_entry(base.get("model") or {}, hour, int(p.get("min_hour_samples", 8)))
    if entry is None:
        return _held(key, "hour_not_learned", **kw)
    expected, sigma, n = entry
    sigma = max(sigma, float(p.get("sigma_floor", 0.0)), 1e-9)
    x = sum(s.v for s in recent) / len(recent)
    z = (x - expected) / sigma

    prev = ri.x(key) if callable(ri.x) else {}
    st = step_stats(prev if key in dev.fresh or prev else None, z,
                    alpha=float(p.get("ewma_alpha", 0.3)), k=float(p.get("cusum_k", 0.5))) \
        if key in dev.fresh or not prev else {k_: prev[k_] for k_ in ("e", "sp", "sn")}
    z_open = float(p.get("z_open", 4.0))
    cond = tripped(st, z_open=z_open, h=float(p.get("cusum_h", 5.0)), direction=direction)
    band = z_open * sigma
    word = "above" if x > expected else "below"
    return Verdict(
        key[0], key[1], cond, observed=x, expected=expected,
        threshold=expected + band if x >= expected else expected - band,
        score=st["e"], diagnosis="unlike_its_usual",
        expr=(f"ewma(z) beyond +/-{z_open} or cusum > {p.get('cusum_h', 5.0)}, "
              f"z = ({key[0]} - expected[hour]) / sigma[hour]"),
        summary=(f"{dev.name} {what} at {_fmt(x)}{unit}, {word} its usual "
                 f"{_fmt(expected)}{unit} (±{_fmt(band)}) for {hour:02d}:00 UTC"),
        x={**st, "n_hour": n}, **kw)


def _resumed_at(samples: list, now: float) -> float | None:
    """When the current unbroken run of samples began, if it followed a gap."""
    if not samples:
        return None
    t = samples[-1].ts
    for s in reversed(samples[:-1]):
        if t - s.ts > GAP_S:
            return t
        t = s.ts
    # The window only holds the last half hour; a run that fills it began
    # at least that long ago, which is past any cold start.
    return None if now - samples[0].ts >= GAP_S else samples[0].ts


def _fmt(v: float) -> str:
    if not math.isfinite(v):
        return "?"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}"


# --- the Phase 3a profile detectors (docs/35 §5) --------------------------------

def sv3(ri: RuleInput) -> list[Verdict]:
    return score_profile(ri, "cpu_utilization", unit=" %", what="CPU")


def up3(ri: RuleInput) -> list[Verdict]:
    return score_profile(ri, "load_pct", unit=" %", what="load")


def pd2(ri: RuleInput) -> list[Verdict]:
    return score_profile(ri, "phase_imbalance_pct", unit=" %", what="phase imbalance",
                         direction="high")


RULES: dict[str, Callable[[RuleInput], list[Verdict]]] = {
    "SV-3": sv3, "UP-3": up3, "PD-2": pd2,
}


def run_rule(ri: RuleInput) -> list[Verdict]:
    fn = RULES.get(ri.detector_id)
    return fn(ri) if fn else []
