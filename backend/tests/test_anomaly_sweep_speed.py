"""The sweep's shortcuts give the answers the full computation does.

Profiled on the live estate a sweep took 13.6 s, most of it re-judging data
already judged: the spike filter re-ran over every sample of every series, and
every saved window was decoded and re-encoded whether or not anything new had
arrived. These pin the shortcuts to the full results.
"""

from __future__ import annotations

import random

from app.analytics import window as win_store
from app.analytics.detectors import quality as q
from app.analytics.detectors.rules import DeviceView, DwellState, Sample
from tests.anomaly_fakes import seed_params

P4 = seed_params("DQ-4")


def _values(n=60, seed=3):
    rnd = random.Random(seed)
    vals = [22.0 + rnd.uniform(-0.2, 0.2) for _ in range(n)]
    for i in (9, 23, 41, 52):
        vals[i] += 6.0                       # four clear spikes
    return vals


def test_judging_only_the_tail_finds_the_same_spikes_there():
    vals = _values()
    full = q.spike_indices(vals, window=11, k=5.0, floor=0.1, rel_floor=0.02)
    for start in (0, 20, 40, 55):
        tail = q.spike_indices(vals, window=11, k=5.0, floor=0.1, rel_floor=0.02, start=start)
        assert tail == [i for i in full if i >= start]


def test_impossible_values_are_judged_the_same_way():
    p = seed_params("DQ-3")
    test = q.impossible_test("inlet_temperature", p)
    for v in (-127.0, 85.0, 3.0, 22.0, 51.0, 327.67):
        assert test(v) == q.is_impossible("inlet_temperature", v, p)


def _evaluate(dev, st):
    return q.evaluate(dev, {"DQ-4": P4}, dev.num[("inlet_temperature", "")][-1].ts,
                      lambda did, key: st, ("inlet_temperature",))


def test_spikes_judged_last_sweep_stay_masked_this_sweep():
    vals = _values()
    t0 = 1_800_000_000.0
    samples = [Sample(t0 + 60 * i, v) for i, v in enumerate(vals)]
    st = DwellState()
    # First sweep sees the first 45 samples; second sweep the rest.
    dev = DeviceView(device_id="d", name="S", device_type="sensor")
    dev.num[("inlet_temperature", "")] = samples[:45]
    for _did, v in _evaluate(dev, st):
        st.x = v.x or st.x
    first_masked = set(dev.dropped.get(("inlet_temperature", ""), ()))
    dev2 = DeviceView(device_id="d", name="S", device_type="sensor")
    dev2.num[("inlet_temperature", "")] = samples
    for _did, v in _evaluate(dev2, st):
        st.x = v.x or st.x
    second = set(dev2.dropped.get(("inlet_temperature", ""), ()))
    # Spikes found on the first pass are still masked on the second, and the
    # ones that arrived since are found.
    assert first_masked <= second
    assert {samples[i].ts for i in (9, 23, 41, 52)} <= second


def test_an_unchanged_window_is_decoded_once():
    win_store.reset_memory()
    nw = win_store.NumWindow()
    nw, _ = win_store.merge_numeric(nw, [(1.0, 22.0, "good"), (61.0, 22.1, "good")], 100.0)
    raw = nw.to_wire()
    a = win_store.parse_cached("d|1|", raw)
    b = win_store.parse_cached("d|1|", raw)
    assert a is b                                    # the second read reused the first
    c = win_store.parse_cached("d|1|", raw.replace("22.1", "23.0"))
    assert c is not a                                # another writer changed it: re-read
