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
from app.analytics.window import KEEP_MAX

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
    return [_profile_one(ri, key, unit=unit, what=what, direction=direction) for key in keys]


def _pre(ri: RuleInput, key: Key, extra: tuple[Key, ...] = ()
         ) -> tuple[Verdict | None, dict[str, Any] | None, list, dict[str, Any]]:
    """The checks every L3 detector makes before it can judge: gates, masks,
    a usable baseline, enough recent samples, not in a cold start.
    Returns (verdict-if-it-cannot-judge, baseline, recent samples, kw)."""
    p, dev, now = ri.params, ri.dev, ri.now
    kw: dict[str, Any] = {"signals": (key, *extra), "fresh": key in dev.fresh}
    gate = context_gate(p, dev)
    if gate:
        return Verdict(key[0], key[1], False, gate=gate, **kw), None, [], kw
    if not dev.online:
        return Verdict(key[0], key[1], False, gate="offline", **kw), None, [], kw
    if key in dev.masked or any(k in dev.masked for k in extra):
        return _held(key, "masked", **kw), None, [], kw
    base = ri.baseline(key)
    if base is None:
        return _held(key, "no_baseline", **kw), None, [], kw
    if base.get("status") != "ok":
        return _held(key, "learning", **kw), None, [], kw
    window = float(p.get("score_window_s", 1800))
    all_clean = dev.clean(key)
    recent = [s for s in all_clean if s.ts >= now - window]
    if len(recent) < int(p.get("min_samples", 3)):
        return _held(key, "too_few_samples", **kw), None, [], kw
    # Back from a gap: the first stretch after the series resumes is a cold
    # start (collectors reconnecting, walks resuming), not behaviour.
    started = _resumed_at(all_clean, now)
    if started is not None and now - started < float(p.get("coldstart_s", 1800)):
        return _held(key, "cold_start", **kw), None, [], kw
    return None, base, recent, kw


def _mean(samples: list) -> float:
    return sum(s.v for s in samples) / len(samples)


def _band(ri: RuleInput, sigma: float) -> float:
    return float(ri.params.get("z_open", 4.0)) * max(sigma, float(ri.params.get("sigma_floor", 0)))


def _judge(ri: RuleInput, key: Key, *, observed: float, expected: float, sigma: float,
           direction: str, diagnosis: str, expr: str, summary: str,
           kw: dict[str, Any], extra_x: dict[str, Any] | None = None) -> Verdict:
    """The shared core: z, EWMA and CUSUM through DwellState.x, and the verdict."""
    p, dev = ri.params, ri.dev
    sigma = max(sigma, float(p.get("sigma_floor", 0.0)), 1e-9)
    z = (observed - expected) / sigma
    prev = dict(ri.x(key) or {})
    if key in dev.fresh or "e" not in prev:
        # A new sample moves the statistics; a sweep with nothing new repeats
        # them, so polling faster than the sweep cannot inflate the CUSUM.
        st = step_stats(prev, z, alpha=float(p.get("ewma_alpha", 0.3)),
                        k=float(p.get("cusum_k", 0.5)))
    else:
        st = {k_: float(prev[k_]) for k_ in ("e", "sp", "sn")}
    z_open = float(p.get("z_open", 4.0))
    cond = tripped(st, z_open=z_open, h=float(p.get("cusum_h", 5.0)), direction=direction)
    band = z_open * sigma
    return Verdict(
        key[0], key[1], cond, observed=observed, expected=expected,
        threshold=expected + band if observed >= expected else expected - band,
        score=st["e"], diagnosis=diagnosis, expr=expr, summary=summary,
        x={**st, **(extra_x or {})}, **kw)


def _profile_one(ri: RuleInput, key: Key, *, unit: str, what: str, direction: str) -> Verdict:
    held, base, recent, kw = _pre(ri, key)
    if held is not None:
        return held
    hour = datetime.fromtimestamp(ri.now, UTC).hour
    entry = hour_entry(base.get("model") or {}, hour, int(ri.params.get("min_hour_samples", 8)))
    if entry is None:
        return _held(key, "hour_not_learned", **kw)
    expected, sigma, n = entry
    x = _mean(recent)
    word = "above" if x > expected else "below"
    return _judge(
        ri, key, observed=x, expected=expected, sigma=sigma, direction=direction,
        diagnosis="unlike_its_usual",
        expr=(f"ewma(z) beyond +/-{ri.params.get('z_open', 4.0)} or cusum > "
              f"{ri.params.get('cusum_h', 5.0)}, z = ({key[0]} - expected[hour]) / sigma[hour]"),
        summary=(f"{ri.dev.name} {what} at {_fmt(x)}{unit}, {word} its usual "
                 f"{_fmt(expected)}{unit} (±{_fmt(_band(ri, sigma))}) for {hour:02d}:00 UTC"),
        kw=kw, extra_x={"n_hour": n})


def score_ols(ri: RuleInput, y_metric: str, x_metric: str, *, unit: str, what: str,
              driver: str, direction: str = "both") -> list[Verdict]:
    """y against what a regression on its driver x says it should be."""
    dev, p = ri.dev, ri.params
    key, xkey = dev.pick(y_metric), dev.pick(x_metric)
    held, base, recent, kw = _pre(ri, key, (xkey,))
    if held is not None:
        return [held]
    window = float(p.get("score_window_s", 1800))
    xs = [s for s in dev.clean(xkey) if s.ts >= ri.now - window]
    if len(xs) < int(p.get("min_samples", 3)):
        return [_held(key, "driver_missing", **kw)]
    m = base.get("model") or {}
    a, b = float(m.get("a", 0.0)), float(m.get("b", 0.0))
    x_lo, x_hi = float(m.get("x_lo", -math.inf)), float(m.get("x_hi", math.inf))
    x = _mean(xs)
    # Never extrapolate: a load the fit never saw says nothing about the link.
    margin = float(p.get("x_margin", 0.1)) * max(x_hi - x_lo, 1e-9)
    if not (x_lo - margin <= x <= x_hi + margin):
        return [_held(key, "outside_learned_range", **kw)]
    expected = a + b * x
    y = _mean(recent)
    sigma = float(m.get("sigma", base.get("resid_sigma") or 0.0))
    word = "more" if y > expected else "less"
    return [_judge(
        ri, key, observed=y, expected=expected, sigma=sigma, direction=direction,
        diagnosis="unlike_its_driver",
        expr=f"{y_metric} vs {a:.3g} + {b:.3g} * {x_metric}, residual ewma/cusum",
        summary=(f"{dev.name} {what} {_fmt(y)}{unit}, {word} than the {_fmt(expected)}{unit} "
                 f"(±{_fmt(_band(ri, sigma))}) expected at {_fmt(x)} {driver}"),
        kw=kw, extra_x={"x": round(x, 3)})]


def score_inlet_gap(ri: RuleInput, *, direction: str = "high") -> list[Verdict]:
    """Rack inlet above the room's supply air, against that gap's usual for the hour.

    The gap, not the inlet: a CRAH setpoint raised by 2 K moves both and the
    gap stays, so a planned change is not an anomaly; hot air recirculating
    into one rack widens only that rack's gap.
    """
    dev = ri.dev
    key = dev.pick("inlet_temperature")
    held, base, recent, kw = _pre(ri, key)
    if held is not None:
        return [held]
    supply = ri.ctx.room_supply.get(dev.room_id or "") if ri.ctx is not None else None
    if supply is None:
        return [_held(key, "no_room_supply", **kw)]
    hour = datetime.fromtimestamp(ri.now, UTC).hour
    entry = hour_entry(base.get("model") or {}, hour, int(ri.params.get("min_hour_samples", 8)))
    if entry is None:
        return [_held(key, "hour_not_learned", **kw)]
    expected, sigma, n = entry
    gap = _mean(recent) - supply
    return [_judge(
        ri, key, observed=gap, expected=expected, sigma=sigma, direction=direction,
        diagnosis="recirculation",
        expr="(inlet_temperature - mean(room supply_air_temp)) vs expected[hour]",
        summary=(f"{dev.name} intake {_fmt(gap)} K above the room's supply air, against "
                 f"a usual {_fmt(expected)} K (±{_fmt(_band(ri, sigma))}) for {hour:02d}:00 UTC: "
                 f"hot air reaching this rack's intake"),
        kw=kw, extra_x={"n_hour": n, "supply": round(supply, 2)})]


def _resumed_at(samples: list, now: float) -> float | None:
    """When the current unbroken run of samples began, if there is evidence it
    follows a gap; None when the history is continuous as far as we can see.

    Evidence is either a gap of more than an hour inside the buffer, or a
    buffer that is short because the series only just began (fewer samples
    than the window keeps). A FULL buffer is continuous history, however
    little time it spans: a series polled every 20 s fills it in about 21
    minutes, and treating that as "just resumed" would hold it forever.
    """
    if not samples:
        return None
    t = samples[-1].ts
    for s in reversed(samples[:-1]):
        if t - s.ts > GAP_S:
            return t
        t = s.ts
    if len(samples) >= KEEP_MAX:
        return None
    return samples[0].ts


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


def sv4(ri: RuleInput) -> list[Verdict]:
    return score_ols(ri, "power_draw", "cpu_utilization", unit=" W", what="drawing",
                     driver="% CPU")


def th1(ri: RuleInput) -> list[Verdict]:
    return score_inlet_gap(ri)


RULES: dict[str, Callable[[RuleInput], list[Verdict]]] = {
    "SV-3": sv3, "UP-3": up3, "PD-2": pd2, "SV-4": sv4, "TH-1": th1,
}


def run_rule(ri: RuleInput) -> list[Verdict]:
    fn = RULES.get(ri.detector_id)
    return fn(ri) if fn else []
