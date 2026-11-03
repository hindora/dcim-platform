"""What a device is expected to read: the learned baseline, as a band (docs/35 §7).

The panel and the device charts ask one question of a fitted baseline: "what
is normal here, and how far along is the learning?". This turns a baseline row
(analytics/baselines.fit_profile's model) into that answer. Pure, so the API
handler stays a read and a call.

The band is expected +/- z_open * sigma, the same distance the detector itself
needs before it counts - so a line leaving the band is exactly a line the
detector is watching, and the band never promises more than the rule does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

HOUR_MS = 3_600_000


def _entry(model: dict[str, Any], hour: int, min_samples: int, floor: float
           ) -> tuple[float, float] | None:
    got = (model.get("hod") or {}).get(str(hour))
    if not got or int(got[2]) < min_samples:
        return None
    return float(got[0]), max(float(got[1]), floor)


def view(row: dict[str, Any], params: dict[str, Any], now: datetime, *,
         window: tuple[datetime, datetime] | None = None) -> dict[str, Any]:
    """One series' baseline for the API: status, learning progress, the
    expected value now, and (with a window) the band hour by hour."""
    model = dict(row.get("model") or {})
    z = float(params.get("z_open", 4.0))
    floor = float(params.get("sigma_floor", 0.0))
    min_samples = int(params.get("min_hour_samples", 8))
    out: dict[str, Any] = {
        "status": row.get("status"),
        "learning": row.get("status") != "ok",
        "hours": int(model.get("hours") or 0),
        "min_hours": int(params.get("min_hours", model.get("min_hours", 120))),
        "days": int(model.get("days") or 0),
        "min_days": int(params.get("min_days", 10)),
        "fitted_at": row.get("fitted_at"),
        "z_open": z,
        "now": None,
        "band": [],
    }
    hour = now.astimezone(UTC).hour
    got = _entry(model, hour, min_samples, floor)
    if got is not None:
        mid, sigma = got
        out["now"] = {"hour": hour, "expected": round(mid, 3),
                      "lo": round(mid - z * sigma, 3), "hi": round(mid + z * sigma, 3)}
    if window is not None:
        out["band"] = band(model, window, z=z, floor=floor, min_samples=min_samples)
    return out


def band(model: dict[str, Any], window: tuple[datetime, datetime], *, z: float,
         floor: float, min_samples: int) -> list[list[float]]:
    """[t_ms, lo, expected, hi] at the start and end of every learned hour in
    the window. An hour not learned is simply absent, so the chart breaks the
    band there instead of drawing a promise the baseline cannot make."""
    start, end = window
    t = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    out: list[list[float]] = []
    while t < end:
        got = _entry(model, t.hour, min_samples, floor)
        if got is not None:
            mid, sigma = got
            lo, hi = round(mid - z * sigma, 3), round(mid + z * sigma, 3)
            a = max(t, start)
            b = min(t + timedelta(hours=1), end)
            ms_a = int(a.timestamp() * 1000)
            ms_b = int(b.timestamp() * 1000) - 1
            out.append([ms_a, lo, round(mid, 3), hi])
            out.append([ms_b, lo, round(mid, 3), hi])
        t += timedelta(hours=1)
    return out
