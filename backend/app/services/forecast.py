"""Forecasting: when does this run out, and is the answer worth having at all.

The second half of that sentence is most of the work. A forecast is easy to
produce and easy to produce badly, and a capacity plan built on a bad one is
worse than a capacity plan built on nothing, because it carries a number and a
date and so it gets believed.

Four rules, all of which cost features:

**Nothing below two weeks.** Fewer than fourteen daily points cannot separate a
trend from a weekend. The response says how much history exists and how much is
needed, and returns no numbers - not a wide interval, not a provisional value,
nothing to screenshot.

**Daily aggregates, not raw samples.** Capacity planning cares about the peak
the infrastructure has to carry, so the series is one value per day. Fitting a
trend through minute-level data fits the diurnal cycle and calls it growth.

**Seasonality has to earn its place.** Holt-Winters with a weekly period needs
several complete cycles before the seasonal indices mean anything, and on a
short series it will happily fit noise and produce a confident wrong answer.
So it is only offered at four cycles, and only if it beats a straight line on a
holdout it never saw. Most of the time the straight line wins, which matches
how capacity planning is actually done.

**A point estimate alone is false precision.** IT load grows in steps - a
deployment lands and the floor takes another 40 kW in an afternoon - so the
honest output is a range and a caveat, not a date. Every value here carries a
prediction interval, and the runway is reported as a window.

The one thing this deliberately does NOT do is smooth over a level shift. A new
row of racks is a step change, and a line fitted across a step is wrong on both
sides of it: it under-forecasts before and over-forecasts after. Where a shift
is detected the series is refit from the shift onward, and the response says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Below this there is no forecast of any kind. Two weeks is not enough to be
# confident, it is the point below which the question cannot be asked.
MIN_HISTORY_DAYS = 14

# Business seasonality, which for a datacenter is the working week. Annual
# seasonality is the one that really moves facility power - outdoor air
# temperature drives chiller work - but it needs a year of history to fit, and
# a year of history is exactly what a young platform does not have.
SEASON_DAYS = 7

# Four complete cycles before a seasonal model is even a candidate. At two, the
# seasonal indices are fitted from two observations each.
MIN_SEASONS_HW = 4

# Held back from fitting so the two methods can be compared on data neither has
# seen. In-sample error always favours the model with more parameters, which
# here is always Holt-Winters.
HOLDOUT_DAYS = 7

# A jump larger than this many robust deviations is treated as a step change
# rather than as growth.
LEVEL_SHIFT_SIGMA = 5.0

# Forecasting further ahead than the history is extrapolation dressed as
# analysis. Thirty days of data buys at most a thirty day horizon.
MAX_HORIZON_RATIO = 1.0

INSUFFICIENT = "insufficient_history"
LINEAR = "linear"
HOLT_WINTERS = "holt_winters"

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


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# --- level shifts -------------------------------------------------------------

def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def detect_level_shift(values: list[float]) -> int | None:
    """Index of a step change, or None.

    Compares a full season either side of every candidate split, and scales the
    difference by the median absolute LAG-7 change rather than the day-to-day
    change. Both details are there because of the same failure.

    A day-to-day scale cannot see past seasonality. On a normal working week -
    flat Monday to Friday, fifteen kilowatts lower at the weekend - the Monday
    morning recovery is a jump of 15 against a typical day's change of about 1,
    and a lag-1 test calls it a step change every single week. That is not a
    cosmetic false positive: a detected shift truncates the history to what
    follows it, so a seasonal series would be cut back to at most seven days
    and then refused for having too little history, permanently, on exactly the
    load shape most datacenters have.

    Differencing at the seasonal lag cancels the weekly pattern, and comparing
    whole seasons either side of the split cancels it again. A steady trend
    moves both the scale and the difference together, so it scores about 1 and
    stays quiet; a step moves only the difference.
    """
    n = len(values)
    m = SEASON_DAYS
    if n < 2 * m:
        return None

    # Noise scale with the weekly pattern differenced out.
    scale = median([abs(values[i] - values[i - m]) for i in range(m, n)])

    best_i, best_z = 0, 0.0
    for i in range(m, n - m + 1):
        diff = abs(mean(values[i:i + m]) - mean(values[i - m:i]))
        # A perfectly steady series has no scale to divide by; there any
        # movement at all is a step rather than noise.
        z = diff / scale if scale > 0 else (float("inf") if diff > 0 else 0.0)
        if z > best_z:
            best_i, best_z = i, z

    if best_z < LEVEL_SHIFT_SIGMA:
        return None
    # A step in the last few days leaves nothing to refit on; better to report
    # it and keep the whole series than to fit three points.
    if best_i > n - MIN_HISTORY_DAYS:
        return None
    return best_i


# --- straight line ------------------------------------------------------------

@dataclass
class LinearModel:
    """Ordinary least squares on the day index, with a proper interval.

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


def fit_linear(values: list[float]) -> LinearModel | None:
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


# --- Holt-Winters -------------------------------------------------------------

@dataclass
class HoltWintersModel:
    """Additive triple exponential smoothing, weekly period.

    Additive rather than multiplicative because datacenter load varies by a
    roughly fixed number of kilowatts between weekday and weekend, not by a
    fixed percentage - a multiplicative model makes the seasonal swing grow
    with the trend, which is not what a shift pattern does.

    The interval is an approximation: there is no closed form for a general
    Holt-Winters prediction interval, so it uses the one-step residual spread
    widened by sqrt(h), the random-walk assumption. It errs wide rather than
    narrow, and it is labelled approximate in the response.
    """

    level: float
    trend: float
    season: list[float]
    m: int
    resid_sd: float
    alpha: float
    beta: float
    gamma: float
    fitted: list[float] = field(default_factory=list)

    def predict(self, h: int) -> float:
        """h is 1-based: h=1 is the first day after the history."""
        idx = (h - 1) % self.m
        return self.level + h * self.trend + self.season[idx]

    def interval(self, h: int) -> tuple[float, float]:
        half = 1.96 * self.resid_sd * math.sqrt(max(1, h))
        p = self.predict(h)
        return p - half, p + half


def _hw_pass(values: list[float], m: int, alpha: float, beta: float,
             gamma: float) -> HoltWintersModel | None:
    n = len(values)
    if n < 2 * m:
        return None
    first = values[:m]
    second = values[m:2 * m]
    level = sum(first) / m
    trend = (sum(second) / m - level) / m
    season = [v - level for v in first]
    fitted: list[float] = []
    for i, y in enumerate(values):
        idx = i % m
        prev_season = season[idx]
        forecast = level + trend + prev_season
        fitted.append(forecast)
        last_level = level
        level = alpha * (y - prev_season) + (1 - alpha) * (level + trend)
        trend = beta * (level - last_level) + (1 - beta) * trend
        season[idx] = gamma * (y - level) + (1 - gamma) * prev_season
    # Residuals from the first full season are dominated by initialisation, so
    # they are excluded from the spread rather than being allowed to inflate
    # every interval the model produces.
    resid = [y - f for y, f in zip(values[m:], fitted[m:], strict=True)]
    df = max(1, len(resid) - 3)
    sd = math.sqrt(sum(r * r for r in resid) / df) if resid else 0.0
    # season is indexed by position in the cycle; rotate so index 0 is the day
    # AFTER the history ends, which is what predict() asks for.
    rotated = [season[(n + k) % m] for k in range(m)]
    return HoltWintersModel(level=level, trend=trend, season=rotated, m=m,
                            resid_sd=sd, alpha=alpha, beta=beta, gamma=gamma,
                            fitted=fitted)


def fit_holt_winters(values: list[float], m: int = SEASON_DAYS,
                     ) -> HoltWintersModel | None:
    """Grid search the smoothing constants.

    A coarse grid, minimising in-sample squared error. Optimising these
    properly needs a solver; the grid gets close enough that the choice between
    Holt-Winters and a straight line is decided by the holdout, not by how well
    the parameters were tuned.
    """
    if len(values) < MIN_SEASONS_HW * m:
        return None
    grid = (0.1, 0.3, 0.5, 0.7, 0.9)
    best: HoltWintersModel | None = None
    best_sse = float("inf")
    for a in grid:
        for b in (0.05, 0.1, 0.3):
            for g in grid:
                mdl = _hw_pass(values, m, a, b, g)
                if mdl is None:
                    continue
                sse = sum((y - f) ** 2
                          for y, f in zip(values[m:], mdl.fitted[m:], strict=True))
                if sse < best_sse:
                    best, best_sse = mdl, sse
    return best


# --- choosing between them ----------------------------------------------------

def mean_abs_error(actual: list[float], predicted: list[float]) -> float:
    pairs = list(zip(actual, predicted, strict=True))
    if not pairs:
        return float("inf")
    return sum(abs(a - p) for a, p in pairs) / len(pairs)


def choose(values: list[float]) -> tuple[str, str]:
    """Which method, and why, decided on data the models did not see.

    In-sample fit always favours Holt-Winters - it has three more parameters
    and a seasonal index per day of the week, so it can bend towards anything.
    The last week is therefore held back, both are fitted on the rest, and the
    winner is the one that predicted a week it had never seen.
    """
    n = len(values)
    if n < MIN_HISTORY_DAYS:
        return INSUFFICIENT, (
            f"{n} days of history; {MIN_HISTORY_DAYS} are needed before a "
            f"trend can be told apart from a weekend")
    if n < MIN_SEASONS_HW * SEASON_DAYS:
        return LINEAR, (
            f"straight line; a weekly seasonal model needs "
            f"{MIN_SEASONS_HW * SEASON_DAYS} days ({MIN_SEASONS_HW} full "
            f"cycles) and there are {n}")

    train, test = values[:-HOLDOUT_DAYS], values[-HOLDOUT_DAYS:]
    lin = fit_linear(train)
    hw = fit_holt_winters(train)
    if lin is None:
        return INSUFFICIENT, "series could not be fitted"
    lin_mae = mean_abs_error(
        test, [lin.predict(len(train) + i) for i in range(len(test))])
    if hw is None:
        return LINEAR, f"straight line; holdout error {lin_mae:.2f}"
    hw_mae = mean_abs_error(test, [hw.predict(i + 1) for i in range(len(test))])
    if hw_mae < lin_mae:
        return HOLT_WINTERS, (
            f"weekly seasonal model; it predicted the held-back week better "
            f"than a straight line ({hw_mae:.2f} vs {lin_mae:.2f} mean error)")
    return LINEAR, (
        f"straight line; the seasonal model did not beat it on the held-back "
        f"week ({hw_mae:.2f} vs {lin_mae:.2f} mean error)")


# --- runway -------------------------------------------------------------------

def runway(points: list[dict[str, Any]], capacity: float | None,
           ) -> dict[str, Any]:
    """The day a projection crosses the limit, as a window.

    Reported three ways because they answer different questions: the earliest
    crossing comes from the upper bound and is what a risk register wants, the
    expected crossing is the point estimate, and the latest comes from the
    lower bound. A single date implies a precision that a fortnight of history
    cannot support.

    Without a capacity there is no runway. On this fleet that is the usual
    case - no rack, PDU or RPP carries a power rating - and saying so is the
    difference between a capacity report and a guess.
    """
    if capacity is None or capacity <= 0:
        return {"days": None, "earliest_days": None, "latest_days": None,
                "reason": "no capacity limit is recorded for this scope, so "
                          "there is nothing for the projection to cross"}

    def first_cross(key: str) -> int | None:
        for p in points:
            v = p.get(key)
            if v is not None and v >= capacity:
                return int(p["day"])
        return None

    expected = first_cross("value")
    earliest = first_cross("upper")
    latest = first_cross("lower")
    horizon = int(points[-1]["day"]) if points else 0
    if expected is None:
        return {
            "days": None, "earliest_days": earliest, "latest_days": None,
            "reason": (
                f"the projection stays below {capacity:g} for the whole "
                f"{horizon} day horizon"
                + ("" if earliest is None else
                   f", though the upper bound crosses at day {earliest}")),
        }
    return {
        "days": expected, "earliest_days": earliest, "latest_days": latest,
        "reason": (f"crosses {capacity:g} at about day {expected}"
                   + (f", as early as day {earliest}" if earliest else "")
                   + (f", as late as day {latest}" if latest else
                      ", with the lower bound not crossing inside the horizon")),
    }


# --- top level ----------------------------------------------------------------

def project(values: list[float], *, horizon_days: int,
            capacity: float | None = None, unit: str = "") -> dict[str, Any]:
    """Fit, project, and say what could not be answered.

    ``values`` is one number per day, oldest first, with no gaps.
    """
    n = len(values)
    result: dict[str, Any] = {
        "history_days": n,
        "min_history_days": MIN_HISTORY_DAYS,
        "unit": unit,
        "notes": [],
    }

    if n < MIN_HISTORY_DAYS:
        method, why = choose(values)
        result.update({
            "method": method, "method_reason": why, "points": [],
            "runway": {"days": None, "earliest_days": None, "latest_days": None,
                       "reason": "no forecast was produced"},
            "trend_per_day": None, "r2": None, "capacity": capacity,
        })
        return result

    used = values
    shift = detect_level_shift(values)
    if shift is not None:
        used = values[shift:]
        result["notes"].append(
            f"a step change of {values[shift] - values[shift - 1]:+.1f} {unit} "
            f"was detected {n - shift} days ago - a new deployment looks like "
            f"a vertical jump, not growth. The fit uses only the {len(used)} "
            f"days since it; a line drawn across a step under-forecasts before "
            f"it and over-forecasts after")
        if len(used) < MIN_HISTORY_DAYS:
            result.update({
                "method": INSUFFICIENT,
                "method_reason": (
                    f"only {len(used)} days since the step change, and "
                    f"{MIN_HISTORY_DAYS} are needed; the history before it "
                    f"describes a different configuration"),
                "points": [], "trend_per_day": None, "r2": None,
                "capacity": capacity,
                "runway": {"days": None, "earliest_days": None,
                           "latest_days": None,
                           "reason": "no forecast was produced"},
            })
            return result

    # Horizon is capped by how much history backs it. Asking for a year off a
    # month is not a longer forecast, it is the same forecast with the error
    # bars off the page.
    capped = min(horizon_days, int(len(used) * MAX_HORIZON_RATIO))
    if capped < horizon_days:
        result["notes"].append(
            f"horizon shortened from {horizon_days} to {capped} days - with "
            f"{len(used)} days of history, anything beyond that is "
            f"extrapolation rather than a forecast")

    method, why = choose(used)
    result["method"] = method
    result["method_reason"] = why

    points: list[dict[str, Any]] = []
    if method == HOLT_WINTERS:
        hw = fit_holt_winters(used)
        lin = fit_linear(used)
        if hw is not None:
            for h in range(1, capped + 1):
                lo, hi = hw.interval(h)
                points.append({"day": h, "value": round(hw.predict(h), 2),
                               "lower": round(lo, 2), "upper": round(hi, 2)})
            result["trend_per_day"] = round(hw.trend, 4)
            result["r2"] = round(lin.r2, 3) if lin else None
            result["notes"].append(
                "the interval on a seasonal model has no closed form; this one "
                "assumes errors accumulate like a random walk, so it is "
                "approximate and errs wide")
    else:
        lin = fit_linear(used)
        if lin is not None:
            base = len(used) - 1
            for h in range(1, capped + 1):
                x = base + h
                lo, hi = lin.interval(x)
                points.append({"day": h, "value": round(lin.predict(x), 2),
                               "lower": round(lo, 2), "upper": round(hi, 2)})
            result["trend_per_day"] = round(lin.slope, 4)
            result["r2"] = round(lin.r2, 3)
            if lin.r2 < 0.3:
                result["notes"].append(
                    f"the trend explains only {lin.r2 * 100:.0f}% of the "
                    f"variation, so the direction is weak evidence and the "
                    f"interval is wide for a reason")

    result["points"] = points
    result["runway"] = runway(points, capacity)
    result["capacity"] = capacity
    return result
