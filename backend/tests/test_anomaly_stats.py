"""app/analytics/stats.py: the pure-Python statistics the detectors score with.

docs/33 Phase 0. EWMA against its closed form; CUSUM at the textbook
k = 0.5 sigma, h = 5 sigma; Hampel flags a spike but not a step; robust z
exact; OLS to 1e-9 with singular designs refused; PCA recovers a known
eigenvector. tests/test_forecast.py, unchanged, is the proof the lift of the
forecaster's helpers changed nothing.
"""

from __future__ import annotations

import math
import random

import pytest

from app.analytics import stats
from app.services import forecast

# --- the lift --------------------------------------------------------------------


def test_the_forecaster_uses_the_lifted_functions():
    assert forecast.fit_linear is stats.fit_linear
    assert forecast.t95 is stats.t95
    assert forecast.LinearModel is stats.LinearModel
    assert forecast.median is stats.median


# --- EWMA ------------------------------------------------------------------------


def test_ewma_matches_its_closed_form():
    lam, z0 = 0.2, 10.0
    xs = [3.0, 7.5, -1.0, 4.25, 12.0, 0.5, 8.0]
    path = stats.ewma(xs, lam, init=z0)
    n = len(xs)
    closed = (1 - lam) ** n * z0 + lam * sum(
        (1 - lam) ** (n - 1 - i) * x for i, x in enumerate(xs))
    assert path[-1] == pytest.approx(closed, abs=1e-12)
    assert len(path) == n


def test_ewma_without_init_starts_at_the_first_sample():
    assert stats.ewma([5.0, 5.0], 0.3) == [5.0, 5.0]


def test_ewma_step_agrees_with_the_path_and_tracks_variance():
    lam = 0.1
    m, v = None, 0.0
    rng = random.Random(7)
    xs = [rng.gauss(20.0, 2.0) for _ in range(20000)]
    vs = []
    for x in xs:
        m, v = stats.ewma_step(m, v, x, lam)
        vs.append(v)
    assert m == pytest.approx(stats.ewma(xs, lam)[-1], abs=1e-9)
    # One reading of an EW variance with lam = 0.1 is a ~19-sample estimate
    # and wanders; its long-run mean on N(20, 2) noise sits near 4.
    tail = vs[100:]
    assert 3.4 < sum(tail) / len(tail) < 4.4


def test_ewma_refuses_a_bad_lambda():
    with pytest.raises(ValueError):
        stats.ewma([1.0], 0.0)


# --- CUSUM -----------------------------------------------------------------------


def test_cusum_signals_a_one_sigma_step_within_6_to_16_samples():
    """k = 0.5 sigma, h = 5 sigma. On a noiseless +1 sigma step C+ grows by
    0.5 sigma per sample and first exceeds 5 sigma at the 11th; with noise the
    textbook ARL is about 10.4. Both must land in 6..16."""
    step = [1.0] * 40
    assert stats.cusum_first_signal(step, target=0.0, sigma=1.0) + 1 == 11

    rng = random.Random(20261021)
    delays = []
    for _ in range(200):
        xs = [rng.gauss(1.0, 1.0) for _ in range(200)]
        i = stats.cusum_first_signal(xs, target=0.0, sigma=1.0)
        assert i is not None
        delays.append(i + 1)
    assert 6 <= stats.median(delays) <= 16


def test_cusum_stays_silent_on_a_seeded_in_control_series():
    rng = random.Random(0)
    xs = [rng.gauss(0.0, 1.0) for _ in range(300)]
    assert stats.cusum_first_signal(xs, target=0.0, sigma=1.0) is None


def test_cusum_in_control_run_length_is_the_textbook_one():
    """Why the silent series above is a seeded one: at k = 0.5, h = 5 the
    two-sided in-control ARL is ~465, so roughly 45% of 300-sample in-control
    series signal somewhere. The Phase 3 tuning (docs/33 3.7) has to live with
    that rate; this pins it so a change to the recursion cannot hide."""
    lengths = []
    for seed in range(300):
        rng = random.Random(1000 + seed)
        pos = neg = 0.0
        n = 0
        while n < 5000:
            n += 1
            st = stats.cusum_step(pos, neg, rng.gauss(0.0, 1.0), target=0.0, sigma=1.0)
            pos, neg = st.pos, st.neg
            if st.signal:
                break
        lengths.append(n)
    assert 350 < sum(lengths) / len(lengths) < 700


def test_cusum_is_two_sided():
    assert stats.cusum_first_signal([-1.0] * 40, target=0.0, sigma=1.0) == 10
    st = stats.cusum_step(0.0, 4.9, -1.0, target=0.0, sigma=1.0)
    assert st.signal == -1 and st.pos == 0.0


def test_cusum_refuses_a_zero_sigma():
    with pytest.raises(ValueError):
        stats.cusum_step(0.0, 0.0, 1.0, target=0.0, sigma=0.0)


# --- Hampel / robust z -----------------------------------------------------------


def test_hampel_flags_a_spike_but_not_a_step():
    # Deterministic ripple rather than Gaussian noise: a 7-sample MAD is
    # itself noisy, and on white noise a 3-sigma Hampel at that window flags
    # the odd innocent point. The property under test is spike-versus-step.
    base = [20.0 + 0.1 * math.sin(i * 1.7) for i in range(60)]
    spiky = list(base)
    spiky[30] = 35.0
    assert stats.hampel(spiky, half_window=3) == [30]

    step = [v + (8.0 if i >= 30 else 0.0) for i, v in enumerate(base)]
    assert stats.hampel(step, half_window=3) == []


def test_robust_z_is_exact():
    xs = [1.0, 2.0, 3.0, 4.0, 100.0]
    # median 3, |x-3| = 2,1,0,1,97 -> MAD 1 -> scale 1.4826
    assert stats.mad(xs) == 1.0
    assert stats.robust_z(100.0, xs) == pytest.approx(97.0 / 1.4826, abs=1e-12)
    assert stats.robust_z(3.0, xs) == 0.0


def test_robust_z_has_no_answer_without_spread():
    assert stats.robust_z(5.0, [2.0, 2.0, 2.0]) is None
    assert stats.robust_z(5.0, []) is None


# --- OLS -------------------------------------------------------------------------


def test_ols_recovers_exact_coefficients():
    rng = random.Random(11)
    rows = [[rng.uniform(0, 500), rng.uniform(5, 30), rng.uniform(-1, 1)] for _ in range(50)]
    y = [1.5 + 0.02 * a - 3.0 * b + 7.25 * c for a, b, c in rows]
    fit = stats.ols(rows, y)
    assert fit.intercept == pytest.approx(1.5, abs=1e-9)
    assert fit.coef[0] == pytest.approx(0.02, abs=1e-9)
    assert fit.coef[1] == pytest.approx(-3.0, abs=1e-9)
    assert fit.coef[2] == pytest.approx(7.25, abs=1e-9)
    assert fit.r2 == pytest.approx(1.0, abs=1e-9)
    assert fit.predict([100.0, 10.0, 0.0]) == pytest.approx(1.5 + 2.0 - 30.0, abs=1e-9)


def test_ols_with_noise_matches_the_simple_regression_closed_form():
    rng = random.Random(5)
    xs = [float(i) for i in range(40)]
    y = [2.0 + 0.5 * x + rng.gauss(0, 1) for x in xs]
    fit = stats.ols([[x] for x in xs], y)
    lin = stats.fit_linear(y)
    assert fit.coef[0] == pytest.approx(lin.slope, abs=1e-9)
    assert fit.intercept == pytest.approx(lin.intercept, abs=1e-9)
    assert fit.resid_sigma == pytest.approx(lin.resid_sd, abs=1e-9)


def test_ols_refuses_a_singular_design():
    rows = [[float(i), 2.0 * i + 1.0] for i in range(20)]  # collinear
    with pytest.raises(stats.SingularDesignError):
        stats.ols(rows, [float(i) for i in range(20)])
    with pytest.raises(stats.SingularDesignError):  # a constant regressor
        stats.ols([[float(i), 3.0] for i in range(20)], [float(i) for i in range(20)])


def test_ols_refuses_more_than_six_regressors():
    rows = [[float(i + j) ** 1.1 for j in range(7)] for i in range(30)]
    with pytest.raises(ValueError, match="at most 6"):
        stats.ols(rows, [1.0] * 30)


# --- PCA -------------------------------------------------------------------------


def test_power_iteration_recovers_a_3x3_eigenvector():
    # A = Q diag(5, 2, 0.5) Q^T with Q's first column (1, 2, 2)/3.
    q = [[1 / 3, 2 / 3, 2 / 3], [2 / 3, 1 / 3, -2 / 3], [2 / 3, -2 / 3, 1 / 3]]
    lam = [5.0, 2.0, 0.5]
    a = [[sum(q[i][k] * lam[k] * q[j][k] for k in range(3)) for j in range(3)]
         for i in range(3)]
    comps = stats.power_iteration(a, 2)
    assert comps[0].eigenvalue == pytest.approx(5.0, abs=1e-9)
    for got, want in zip(comps[0].vector, (1 / 3, 2 / 3, 2 / 3), strict=True):
        assert got == pytest.approx(want, abs=1e-6)
    assert comps[1].eigenvalue == pytest.approx(2.0, abs=1e-6)
    assert math.isclose(sum(v * v for v in comps[0].vector), 1.0, abs_tol=1e-12)


def test_pca_finds_the_direction_of_correlated_data():
    rng = random.Random(9)
    rows = []
    for _ in range(500):
        t = rng.gauss(0, 3)
        rows.append([t + rng.gauss(0, 0.1), t + rng.gauss(0, 0.1)])
    v = stats.pca(rows)[0].vector
    assert v[0] == pytest.approx(math.sqrt(0.5), abs=0.01)
    assert v[1] == pytest.approx(math.sqrt(0.5), abs=0.01)
