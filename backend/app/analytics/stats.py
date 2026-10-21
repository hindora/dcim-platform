"""Small, dependency-free statistics for the anomaly layer and the forecaster.

Pure Python by rule (docs/33 section 1, item 7): no numpy, pandas or scipy.
The series this scores are short - a 30-minute window, a few hundred hourly
buckets, at most six regressors - so clarity beats vectorisation, and a
dependency the ingest worker does not otherwise need is a deploy risk on a
host that is already short of memory.

The first block is lifted unchanged from services/forecast.py, which now
imports it from here; tests/test_forecast.py is the proof the lift changed
nothing. The rest is what the detectors need:

  EWMA            exponentially weighted mean, and its variance
  CUSUM           Page's two-sided tabular CUSUM, k and h in sigma units
  Hampel          sliding-window median/MAD outlier flag
  robust z / MAD  median-centred z with the 1.4826 consistency constant
  OLS             normal equations, at most six regressors, singular refused
  PCA             power iteration with deflation on a covariance matrix
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# --- lifted from services/forecast.py ------------------------------------------

# Two-sided 95% Student-t quantiles. A small table beats a dependency on scipy
# for the one number needed, and beats hardcoding 1.96 - at 12 degrees of
# freedom that understates the interval by about 11%, which is the difference
# between "we have a year" and "we have nine months".
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


def t95(df: int) -> float:
    """95% two-sided t quantile, exact to 30 df and approximated beyond."""
    if df <= 0:
        return float("inf")
    if df in _T95:
        return _T95[df]
    # Decays towards the normal 1.96; within about 0.005 over 30 < df < 300.
    return 1.96 + (2.042 - 1.96) * (30.0 / df)


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def detect_level_shift(values: Sequence[float], *, season: int, sigma: float,
                       min_tail: int) -> int | None:
    """Index of a step change, or None.

    Compares a full season either side of every candidate split, scaled by the
    median absolute lag-`season` change, so a weekly pattern is differenced
    out and does not read as a step every Monday. A steady trend moves the
    scale and the difference together and stays quiet; a step moves only the
    difference. A step later than `min_tail` from the end is not reported: it
    leaves too little to refit on. See services/forecast.detect_level_shift
    for the full rationale; this is its body with the constants as arguments.
    """
    n = len(values)
    m = season
    if n < 2 * m:
        return None

    scale = median([abs(values[i] - values[i - m]) for i in range(m, n)])

    best_i, best_z = 0, 0.0
    for i in range(m, n - m + 1):
        diff = abs(mean(values[i:i + m]) - mean(values[i - m:i]))
        # A perfectly steady series has no scale to divide by; there any
        # movement at all is a step rather than noise.
        z = diff / scale if scale > 0 else (float("inf") if diff > 0 else 0.0)
        if z > best_z:
            best_i, best_z = i, z

    if best_z < sigma:
        return None
    if best_i > n - min_tail:
        return None
    return best_i


@dataclass
class LinearModel:
    """Ordinary least squares on the index, with a proper interval.

    The interval is the textbook prediction interval, not a confidence interval
    on the mean: it widens with distance from the centre of the data, which is
    the behaviour that stops a 90-day extrapolation from looking as solid as a
    5-day one.
    """

    slope: float
    intercept: float
    n: int
    x_mean: float
    sxx: float
    resid_sd: float
    r2: float

    def predict(self, x: float) -> float:
        return self.intercept + self.slope * x

    def interval(self, x: float) -> tuple[float, float]:
        df = self.n - 2
        if df <= 0 or self.sxx <= 0:
            p = self.predict(x)
            return p, p
        se = self.resid_sd * math.sqrt(
            1.0 + 1.0 / self.n + (x - self.x_mean) ** 2 / self.sxx)
        half = t95(df) * se
        p = self.predict(x)
        return p - half, p + half


def fit_linear(values: Sequence[float]) -> LinearModel | None:
    n = len(values)
    if n < 3:
        return None
    xs = [float(i) for i in range(n)]
    x_mean = sum(xs) / n
    y_mean = sum(values) / n
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values, strict=True))
    slope = sxy / sxx
    intercept = y_mean - slope * x_mean
    resid = [y - (intercept + slope * x) for x, y in zip(xs, values, strict=True)]
    sse = sum(r * r for r in resid)
    sst = sum((y - y_mean) ** 2 for y in values)
    df = n - 2
    return LinearModel(
        slope=slope, intercept=intercept, n=n, x_mean=x_mean, sxx=sxx,
        resid_sd=math.sqrt(sse / df) if df > 0 else 0.0,
        # A flat series has no variance to explain; calling that a perfect fit
        # is arithmetically true and practically misleading, so it reads 0.
        r2=(1.0 - sse / sst) if sst > 0 else 0.0)


# --- robust location and scale ----------------------------------------------

#: Makes the MAD a consistent estimator of sigma for normal data.
MAD_TO_SIGMA = 1.4826


def mad(xs: Sequence[float]) -> float:
    """Median absolute deviation from the median (unscaled)."""
    if not xs:
        return 0.0
    m = median(xs)
    return median([abs(x - m) for x in xs])


def robust_z(x: float, xs: Sequence[float]) -> float | None:
    """(x - median) / (1.4826 * MAD), or None when the scale is zero.

    None rather than infinity: a peer group whose members all read the same
    value has no spread to judge against, and the caller decides what that
    means (a flat-lined sensor group is itself a data-quality question).
    """
    if not xs:
        return None
    scale = MAD_TO_SIGMA * mad(xs)
    if scale <= 0.0:
        return None
    return (x - median(xs)) / scale


def hampel(values: Sequence[float], half_window: int = 3,
           n_sigma: float = 3.0) -> list[int]:
    """Indices that are Hampel outliers.

    Each point is compared with the median of the window centred on it
    (truncated at the ends) and flagged when it sits more than `n_sigma`
    robust sigmas away. A spike is flagged; a step is not, because on either
    side of a step the majority of the window - and so its median - is on the
    point's own level. A window with zero spread flags any point that differs
    from its median at all.
    """
    if half_window < 1:
        raise ValueError("half_window must be >= 1")
    out: list[int] = []
    n = len(values)
    for i in range(n):
        lo, hi = max(0, i - half_window), min(n, i + half_window + 1)
        win = values[lo:hi]
        m = median(win)
        s = MAD_TO_SIGMA * mad(win)
        dev = abs(values[i] - m)
        if dev > n_sigma * s and dev > 0.0:
            out.append(i)
    return out


# --- EWMA -------------------------------------------------------------------

def ewma(values: Sequence[float], lam: float, init: float | None = None) -> list[float]:
    """The EWMA path z_t = lam * x_t + (1 - lam) * z_{t-1}.

    `init` is z_0, the value before the first sample; None starts at the first
    sample (z_1 = x_1). Closed form: z_n = (1-lam)^n z_0 + lam * sum
    (1-lam)^(n-i) x_i.
    """
    if not 0.0 < lam <= 1.0:
        raise ValueError("lam must be in (0, 1]")
    out: list[float] = []
    z = init
    for x in values:
        z = x if z is None else lam * x + (1.0 - lam) * z
        out.append(z)
    return out


def ewma_step(mean_: float | None, var: float, x: float,
              lam: float) -> tuple[float, float]:
    """One incremental update of an exponentially weighted mean and variance.

    The variance recursion is the standard one (Finch 2009, "Incremental
    calculation of weighted mean and variance"): var' = (1-lam)(var + lam d^2)
    with d = x - mean. A None mean starts the accumulator at x with zero
    variance. This is the state shape docs/29 section 7.2 checkpoints
    ({"ewma", "ewma_var"}).
    """
    if not 0.0 < lam <= 1.0:
        raise ValueError("lam must be in (0, 1]")
    if mean_ is None:
        return x, 0.0
    d = x - mean_
    incr = lam * d
    return mean_ + incr, (1.0 - lam) * (var + d * incr)


# --- CUSUM ------------------------------------------------------------------

@dataclass(frozen=True)
class CusumStep:
    pos: float
    neg: float
    #: +1 an upward shift signalled, -1 downward, 0 none.
    signal: int


def cusum_step(pos: float, neg: float, x: float, *, target: float, sigma: float,
               k_sigma: float = 0.5, h_sigma: float = 5.0) -> CusumStep:
    """One step of Page's two-sided tabular CUSUM.

    C+ = max(0, C+ + (x - mu) - k),  C- = max(0, C- - (x - mu) - k), with
    k = k_sigma * sigma (the allowance, half the shift to detect) and the
    signal at C > h = h_sigma * sigma. The textbook k = 0.5 sigma, h = 5 sigma
    has an in-control ARL near 465 and detects a 1 sigma shift in about 10
    samples (Montgomery, Introduction to Statistical Quality Control, the
    tabular CUSUM ARL table). The accumulators are NOT reset on a signal
    here: the caller owns the finding lifecycle and resets deliberately.
    """
    if sigma <= 0.0:
        raise ValueError("sigma must be > 0")
    k = k_sigma * sigma
    h = h_sigma * sigma
    d = x - target
    pos = max(0.0, pos + d - k)
    neg = max(0.0, neg - d - k)
    sig = 1 if pos > h else (-1 if neg > h else 0)
    return CusumStep(pos, neg, sig)


def cusum_first_signal(values: Sequence[float], *, target: float, sigma: float,
                       k_sigma: float = 0.5, h_sigma: float = 5.0) -> int | None:
    """Index of the first sample at which the two-sided CUSUM signals."""
    pos = neg = 0.0
    for i, x in enumerate(values):
        st = cusum_step(pos, neg, x, target=target, sigma=sigma,
                        k_sigma=k_sigma, h_sigma=h_sigma)
        if st.signal:
            return i
        pos, neg = st.pos, st.neg
    return None


# --- OLS --------------------------------------------------------------------

#: Baselines regress on at most six drivers (docs/28: load, wet bulb, ...).
#: More than that on a few hundred hourly buckets is fitting noise.
MAX_REGRESSORS = 6

#: Relative pivot below which the (standardised) normal matrix is singular.
_SINGULAR_EPS = 1e-10


class SingularDesignError(ValueError):
    """The regressors are collinear (or one is constant) and the fit is refused."""


@dataclass(frozen=True)
class OlsFit:
    intercept: float
    coef: tuple[float, ...]
    n: int
    r2: float
    #: Residual standard deviation with n - p - 1 degrees of freedom.
    resid_sigma: float

    def predict(self, x: Sequence[float]) -> float:
        if len(x) != len(self.coef):
            raise ValueError("wrong number of regressors")
        return self.intercept + sum(c * v for c, v in zip(self.coef, x, strict=True))


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; refuses a singular matrix.

    `a` is a correlation matrix (unit diagonal), so an absolute pivot
    threshold is also a relative one.
    """
    n = len(a)
    m = [[*row, b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < _SINGULAR_EPS:
            raise SingularDesignError("singular design: regressors are collinear")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / m[r][r]
    return x


def ols(rows: Sequence[Sequence[float]], y: Sequence[float]) -> OlsFit:
    """Least squares y = b0 + sum b_j x_j through the normal equations.

    The columns are centred and scaled before X'X is formed, so the matrix
    solved is the regressors' correlation matrix. That keeps the squared
    condition number of the normal equations tolerable for plant data whose
    regressors differ by orders of magnitude (kW against degrees), and it
    makes the singularity test scale-free: a constant regressor or an exact
    linear combination is refused with SingularDesignError rather than
    returned as huge offsetting coefficients.
    """
    n = len(y)
    if n != len(rows):
        raise ValueError("rows and y differ in length")
    p = len(rows[0]) if rows else 0
    if p > MAX_REGRESSORS:
        raise ValueError(f"at most {MAX_REGRESSORS} regressors, got {p}")
    if any(len(r) != p for r in rows):
        raise ValueError("ragged design")
    if n < p + 2:
        raise ValueError(f"need at least {p + 2} rows for {p} regressors, got {n}")

    y_mean = sum(y) / n
    sst = sum((v - y_mean) ** 2 for v in y)
    if p == 0:
        sd = math.sqrt(sst / (n - 1)) if n > 1 else 0.0
        return OlsFit(intercept=y_mean, coef=(), n=n, r2=0.0, resid_sigma=sd)

    means = [sum(r[j] for r in rows) / n for j in range(p)]
    scales = []
    for j in range(p):
        ss = sum((r[j] - means[j]) ** 2 for r in rows)
        if ss <= 0.0:
            raise SingularDesignError(f"singular design: regressor {j} is constant")
        scales.append(math.sqrt(ss))
    z = [[(r[j] - means[j]) / scales[j] for j in range(p)] for r in rows]
    xtx = [[sum(zr[i] * zr[j] for zr in z) for j in range(p)] for i in range(p)]
    xty = [sum(zr[i] * (v - y_mean) for zr, v in zip(z, y, strict=True)) for i in range(p)]
    beta_z = _solve(xtx, xty)
    coef = tuple(beta_z[j] / scales[j] for j in range(p))
    intercept = y_mean - sum(c * m for c, m in zip(coef, means, strict=True))

    sse = 0.0
    for r, v in zip(rows, y, strict=True):
        e = v - (intercept + sum(c * x for c, x in zip(coef, r, strict=True)))
        sse += e * e
    df = n - p - 1
    return OlsFit(
        intercept=intercept, coef=coef, n=n,
        r2=(1.0 - sse / sst) if sst > 0 else 0.0,
        resid_sigma=math.sqrt(sse / df) if df > 0 else 0.0)


# --- PCA --------------------------------------------------------------------

def covariance(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    """Sample covariance matrix (n - 1 denominator) of the columns of `rows`."""
    n = len(rows)
    if n < 2:
        raise ValueError("need at least two rows")
    p = len(rows[0])
    means = [sum(r[j] for r in rows) / n for j in range(p)]
    c = [[0.0] * p for _ in range(p)]
    for r in rows:
        d = [r[j] - means[j] for j in range(p)]
        for i in range(p):
            for j in range(i, p):
                c[i][j] += d[i] * d[j]
    for i in range(p):
        for j in range(i, p):
            c[i][j] /= (n - 1)
            c[j][i] = c[i][j]
    return c


@dataclass(frozen=True)
class Component:
    eigenvalue: float
    #: Unit length; sign fixed so the largest-magnitude element is positive.
    vector: tuple[float, ...]


def _normalise(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm > 0 else v


def power_iteration(matrix: Sequence[Sequence[float]], n_components: int = 1, *,
                    max_iter: int = 1000, tol: float = 1e-12) -> list[Component]:
    """Leading eigenpairs of a symmetric PSD matrix, by power iteration with
    Hotelling deflation.

    The start vector is fixed (not random) so a fit is reproducible, and it is
    deliberately not the all-ones vector, which is orthogonal to a contrast
    eigenvector and would converge to the wrong one.
    """
    p = len(matrix)
    a = [list(map(float, row)) for row in matrix]
    if any(len(row) != p for row in a):
        raise ValueError("matrix must be square")
    out: list[Component] = []
    for _ in range(min(n_components, p)):
        v = _normalise([1.0 / (i + 1) + 0.1 * i for i in range(p)])
        lam = 0.0
        for _it in range(max_iter):
            w = [sum(a[i][j] * v[j] for j in range(p)) for i in range(p)]
            new_lam = sum(v[i] * w[i] for i in range(p))
            w = _normalise(w)
            delta = max(abs(w[i] - v[i]) for i in range(p))
            v, lam = w, new_lam
            if delta < tol:
                break
        big = max(range(p), key=lambda i: abs(v[i]))
        if v[big] < 0:
            v = [-x for x in v]
        out.append(Component(eigenvalue=lam, vector=tuple(v)))
        for i in range(p):
            for j in range(p):
                a[i][j] -= lam * v[i] * v[j]
    return out


def pca(rows: Sequence[Sequence[float]], n_components: int = 1) -> list[Component]:
    """Principal components of the columns of `rows` (covariance, not scaled)."""
    return power_iteration(covariance(rows), n_components)
