"""L0: data quality. Condemn bad data before any rule reasons from it.

Doc 28's first rule of the sensor layer: a fault detector that trusts every
sample turns every sensor failure into a process alert. Production FDD tools
do the same thing first - SkySpark/Clockworks "data quality" rules, the BACnet
`Reliability` property, a historian's quality flags - and so does this.

Five checks per numeric series (docs/29 section 12.1). Each yields a finding
of kind `sensor` in the advisory lane, and each also MASKS the series for
the L2 rules (`DeviceView.masked`) while it holds:

  DQ-1  dropout     no sample within 3 x poll (clamped 300..10800 s) while the
                    endpoint is ONLINE and the device still delivers other
                    series. Endpoint-level silence is telemetry_stale, a
                    different alarm. A Modbus device whose Data_Valid bit is
                    low returns NO samples for the whole poll, so "others
                    delivering" is false and DQ-1 stays silent: the device is
                    not ready, not one sensor gone. OFFLINE holds (never opens,
                    never clears).
  DQ-2  non-GOOD    >= 50 % of >= 5 samples in 1800 s flagged suspect or bad.
                    BACnet samples carry real quality since the Phase 1
                    collector (FAULT / Reliability -> BAD, OUT_OF_SERVICE /
                    OVERRIDDEN -> SUSPECT); ingest marks out-of-registry-range
                    values suspect.
  DQ-3  impossible  outside the physical range, or exactly a known failure
                    sentinel (-40 open thermistor, -127 / 85 DS18B20
                    disconnect / power-on, +/-327.67 int16/100 overflow).
                    Every such sample is dropped from L2 input.
  DQ-4  spikes      Hampel (w = 11, 5 robust sigmas, with an absolute MAD
                    floor so a quiet sensor's 0.1 K wobble is not a spike). A
                    single spike is masked, not reported; a finding opens above
                    max_spikes_per_h.
  DQ-5  flatline    the last 20 samples identical while a COUPLED variable on
                    the same device moved (a CRAH's supply while its valve or
                    fan moves). Silent on a frozen setpoint and on static
                    file-backed SNMP points, which have nothing coupled.

Separately from the five checks, and unconditionally: **a series whose newest
sample is not GOOD is masked**. A point in fault keeps sending its held value
marked BAD; that value is no evidence of anything, so the rules hold rather
than judge it. BAD and SUSPECT samples never reach a rule (DeviceView.clean).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from itertools import pairwise
from typing import Any

from app.analytics.catalogue import UNIT_FAMILY
from app.analytics.detectors.rules import (
    DeviceView,
    DwellState,
    Key,
    Sample,
    Verdict,
)
from app.analytics.stats import MAD_TO_SIGMA, median
from app.core.metrics_gen import METRICS

QUALITY_IDS = ("DQ-1", "DQ-2", "DQ-3", "DQ-4", "DQ-5")


def _family(metric_key: str) -> str | None:
    m = METRICS.get(metric_key)
    return UNIT_FAMILY.get(m.unit) if m else None


def grace_s(p: Mapping[str, Any], interval_s: float | None) -> float:
    """staleness.grace_seconds, per series: 3 x interval, clamped."""
    lo, hi = float(p.get("min_grace_s", 300)), float(p.get("max_grace_s", 10_800))
    if not interval_s:
        return lo
    return max(lo, min(hi, float(p.get("grace_intervals", 3)) * interval_s))


def observed_interval(samples: list[Sample]) -> float | None:
    if len(samples) < 3:
        return None
    gaps = [b.ts - a.ts for a, b in pairwise(samples) if b.ts > a.ts]
    return median(gaps) if gaps else None


def is_impossible(metric_key: str, v: float, p: Mapping[str, Any]) -> bool:
    for s in p.get("sentinels", ()):
        if abs(v - float(s)) < 1e-6:
            return True
    b = (p.get("bounds") or {}).get(metric_key)
    return bool(b) and not (float(b[0]) <= v <= float(b[1]))


def spike_indices(values: list[float], *, window: int, k: float, floor: float,
                  rel_floor: float = 0.0) -> list[int]:
    """Hampel with a scale floor; only points with a later neighbour.

    The floor is the larger of an absolute one (by unit family) and
    `rel_floor` x |window median|. A steady signal reported in coarse steps -
    a CRAH's power draw in 10 W steps round 430 W - has a MAD of ~0, and
    without a floor every ordinary 10 W step reads as a spike. One relative
    floor serves every magnitude a family spans (a 50 W probe to a 200 kW
    panel), which no single absolute number can.

    The newest point cannot be told from the first sample of a step yet, so
    it is masked (see `spikes_to_drop`) but not COUNTED toward a finding.
    """
    half = max(1, window // 2)
    out = []
    n = len(values)
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        win = values[lo:hi]
        if len(win) < 3:
            continue
        m = median(win)
        scale = max(MAD_TO_SIGMA * median([abs(x - m) for x in win]), floor,
                    rel_floor * abs(m))
        if abs(values[i] - m) > k * scale:
            out.append(i)
    return out


def evaluate(dev: DeviceView, params: Mapping[str, Mapping[str, Any]], now: float,
             state: Callable[[str, Key], DwellState],
             scope: tuple[str, ...]) -> list[tuple[str, Verdict]]:
    """Run every enabled DQ check over the device's in-scope numeric series.

    `params` maps the DQ ids effective on this device to their params; a
    missing id is off here. Mutates `dev.masked` and `dev.dropped`.
    Returns (detector_id, verdict) pairs for the dwell framework.
    """
    out: list[tuple[str, Verdict]] = []
    # Unconditional, and over every series, not just the L0 scope: a held
    # value marked BAD is no evidence (see the module docstring).
    for k, s in dev.num.items():
        if s and s[-1].q != "good":
            dev.masked.add(k)
    keys = sorted(k for k in dev.num if k[0] in scope)
    newest = {k: dev.num[k][-1].ts for k in keys if dev.num[k]}

    for key in keys:
        series = dev.num[key]
        if not series:
            continue
        fresh = key in dev.fresh
        last = series[-1]
        kw = {"signals": (key,), "alarm_metric_key": None, "fresh": fresh}
        masked = last.q != "good"

        def verdict(did: str, cond: bool | None, _key: Key = key,
                    _kw: dict = kw, **extra) -> None:
            nonlocal masked
            out.append((did, Verdict(_key[0], _key[1], cond, **_kw, **extra)))
            if cond or state(did, _key).active:
                masked = True

        # --- DQ-1 dropout ---------------------------------------------------
        if (p := params.get("DQ-1")) is not None:
            interval = max(dev.poll_interval_s or 0, observed_interval(series) or 0) or None
            g = grace_s(p, interval)
            silent = now - last.ts
            others = any(now - t <= g for k, t in newest.items() if k != key)
            if not dev.online:
                verdict("DQ-1", None, gate=None)
            else:
                cond = silent > g and others
                verdict("DQ-1", cond, observed=silent, threshold=g, score=silent / g,
                        diagnosis="dropout", expr=f"now - last_sample > {int(g)} s",
                        summary=(f"{dev.name} {key[0]}{_inst(key)} silent for "
                                 f"{int(silent // 60)} min while the device still reports"))

        # --- DQ-2 non-GOOD run ------------------------------------------------
        if (p := params.get("DQ-2")) is not None:
            win = [s for s in series if now - s.ts <= float(p.get("window_s", 1800))]
            if len(win) < int(p.get("min_samples", 5)):
                verdict("DQ-2", False if not win else None)
            else:
                bad = sum(1 for s in win if s.q != "good") / len(win)
                lim = float(p.get("min_bad_fraction", 0.5))
                verdict("DQ-2", bad >= lim, observed=bad, threshold=lim, score=bad / lim,
                        diagnosis="non_good",
                        expr=f"share(quality <> good, {int(p.get('window_s', 1800))} s) "
                             f">= {lim}",
                        summary=(f"{dev.name} {key[0]}{_inst(key)}: {round(bad * 100)}% of "
                                 f"{len(win)} recent samples flagged suspect or bad"))

        # --- DQ-3 impossible values and sentinels --------------------------------
        if (p := params.get("DQ-3")) is not None:
            bad_ts = {s.ts for s in series if s.q == "good" and is_impossible(key[0], s.v, p)}
            if bad_ts:
                dev.dropped.setdefault(key, set()).update(bad_ts)
            good = [s for s in series if s.q == "good"]
            if good:
                cur = good[-1]
                cond = cur.ts in bad_ts
                verdict("DQ-3", cond, observed=cur.v, diagnosis="impossible",
                        expr=f"{key[0]} outside its physical range or a sentinel",
                        summary=(f"{dev.name} {key[0]}{_inst(key)} reads {cur.v:g} - "
                                 f"physically impossible or a failure sentinel"))

        # --- DQ-4 spikes ------------------------------------------------------------
        if (p := params.get("DQ-4")) is not None:
            good = [s for s in series if s.q == "good"
                    and s.ts not in dev.dropped.get(key, ())]
            fam = _family(key[0])
            floor = float((p.get("min_mad_abs") or {}).get(fam or "", 0.0))
            idx = spike_indices([s.v for s in good], window=int(p.get("window", 11)),
                                k=float(p.get("k_mad", 5.0)), floor=floor,
                                rel_floor=float(p.get("min_dev_rel", 0.02)))
            if idx:
                dev.dropped.setdefault(key, set()).update(good[i].ts for i in idx)
            st = state("DQ-4", key)
            seen = [t for t in (st.x.get("sp") or []) if now - t <= 3600]
            confirmed = {round(good[i].ts, 3) for i in idx if i < len(good) - 1}
            seen = sorted(set(seen) | confirmed)
            lim = int(p.get("max_spikes_per_h", 6))
            if len(good) < 5:
                verdict("DQ-4", None, x={"sp": seen})
            else:
                verdict("DQ-4", len(seen) > lim, observed=float(len(seen)),
                        threshold=float(lim), score=len(seen) / lim, diagnosis="spiky",
                        expr=f"hampel_spikes(1 h) > {lim}", x={"sp": seen},
                        summary=(f"{dev.name} {key[0]}{_inst(key)} spiked {len(seen)} times "
                                 f"in the last hour"))

        # --- DQ-5 flatline while a coupled variable moves -------------------------------
        if (p := params.get("DQ-5")) is not None:
            coupled = (p.get("coupled") or {}).get(key[0])
            n = int(p.get("min_samples", 20))
            good = [s for s in series if s.q == "good"]
            if coupled and len(good) >= n:
                tail = good[-n:]
                flat = max(s.v for s in tail) == min(s.v for s in tail)
                moved = None
                for ck, ci in coupled:
                    cs = [s for s in dev.num.get((ck, ci), ()) if s.q == "good"
                          and s.ts >= tail[0].ts]
                    if len(cs) < 2:
                        continue
                    need = float((p.get("coupled_min_range") or {}).get(
                        _family(ck) or "", 0.5))
                    rng = max(s.v for s in cs) - min(s.v for s in cs)
                    if rng >= need:
                        moved = (ck, ci, rng)
                        break
                cond = flat and moved is not None
                verdict("DQ-5", cond, observed=tail[-1].v, diagnosis="flatline",
                        expr=f"var({key[0]}, last {n}) == 0 AND range(coupled) >= min",
                        summary=(f"{dev.name} {key[0]}{_inst(key)} frozen at {tail[-1].v:g} "
                                 f"for {n} samples while "
                                 f"{moved[0] if moved else 'its coupled point'} moved"))
            elif coupled:
                verdict("DQ-5", None)

        if masked:
            dev.masked.add(key)
    return out


def _inst(key: Key) -> str:
    return f"{{{key[1]}}}" if key[1] else ""
