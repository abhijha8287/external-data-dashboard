"""Derived metrics. Pure functions: no I/O, no clock, no database. Trivially unit-testable.

Definitions (also in the README):
  change_abs   latest - previous observation
  change_pct   change_abs / |previous| * 100   (None when previous == 0; see README on Celsius)
  rolling_avg  mean of the last N observations *including* the latest; if fewer than N exist,
               uses what exists and reports `rolling_n_used` so the UI can say so
  min / max    over the trailing `stat_hours` of *time* (not count), with when they occurred
  stdev        sample (n-1) standard deviation over the same N-point window
  slope_per_h  least-squares trend (deg C per hour) over the same N-point window
  rate_z       z-score of the latest rate-of-change vs the preceding rates-of-change.
               Temperature trends naturally (sunrise), so z-scoring the *level* would flag every
               morning; z-scoring the *step* flags only abrupt departures from the recent trend.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

# Rates below this (deg C / hour) are within sensor resolution noise; flooring the stdev stops a
# 0.1 C wobble on an otherwise flat series from producing an absurd z-score.
Z_SD_FLOOR = 0.5
Z_MIN_BASELINE = 5  # need at least this many prior rates to say anything about "normal"


@dataclass(frozen=True)
class Point:
    t: datetime
    v: float


def pct_change(prev: float, cur: float) -> Optional[float]:
    if prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100.0


def mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def sample_stdev(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def slope_per_hour(points: Sequence[Point]) -> Optional[float]:
    """Ordinary least squares slope of value vs time, in units per hour."""
    if len(points) < 2:
        return None
    t0 = points[0].t
    xs = [(p.t - t0).total_seconds() / 3600.0 for p in points]
    ys = [p.v for p in points]
    mx, my = mean(xs), mean(ys)
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def rolling_series(points: Sequence[Point], n: int) -> List[float]:
    """Trailing mean at each point over up to n points (partial window at the start)."""
    vals = [p.v for p in points]
    return [mean(vals[max(0, i - n + 1): i + 1]) for i in range(len(vals))]


def rates_per_hour(points: Sequence[Point]) -> List[float]:
    out = []
    for a, b in zip(points, points[1:]):
        hours = (b.t - a.t).total_seconds() / 3600.0
        if hours > 0:
            out.append((b.v - a.v) / hours)
    return out


def rate_zscore(points: Sequence[Point], n: int) -> Optional[float]:
    rates = rates_per_hour(points)
    if len(rates) < 2:
        return None
    latest, base = rates[-1], rates[-1 - n:-1]
    if len(base) < Z_MIN_BASELINE:
        return None
    sd = max(sample_stdev(base) or 0.0, Z_SD_FLOOR)
    return (latest - mean(base)) / sd


def compute(
    points: Sequence[Point],
    *,
    window: int = 12,
    stat_hours: float = 24.0,
    spike_abs: float = 2.0,
    z_threshold: float = 3.0,
) -> Optional[Dict]:
    """Metrics for a time-ascending series. Returns None if there is no data at all."""
    if not points:
        return None
    if window < 1:
        raise ValueError("window must be >= 1")
    latest = points[-1]
    win = list(points[-window:])

    change_abs = change_pct = prev_v = None
    if len(points) >= 2:
        prev_v = points[-2].v
        change_abs = latest.v - prev_v
        change_pct = pct_change(prev_v, latest.v)

    horizon = latest.t - timedelta(hours=stat_hours)
    in_range = [p for p in points if p.t >= horizon]
    lo = min(in_range, key=lambda p: p.v)
    hi = max(in_range, key=lambda p: p.v)

    z = rate_zscore(points, window)
    alerts = []
    if change_abs is not None and abs(change_abs) >= spike_abs:
        alerts.append({"code": "spike", "message": "Moved %+.1f C since the previous reading (threshold %.1f C)" % (change_abs, spike_abs)})
    if z is not None and abs(z) >= z_threshold:
        alerts.append({"code": "anomaly", "message": "Rate of change is %.1f standard deviations from the recent trend" % z})

    return {
        "count": len(points),
        "previous": prev_v,
        "change_abs": change_abs,
        "change_pct": change_pct,
        "rolling_window": window,
        "rolling_n_used": len(win),
        "rolling_avg": mean([p.v for p in win]),
        "stdev": sample_stdev([p.v for p in win]),
        "slope_per_hour": slope_per_hour(win),
        "stat_hours": stat_hours,
        "stat_n": len(in_range),
        "min": {"value": lo.v, "at": lo.t},
        "max": {"value": hi.v, "at": hi.t},
        "rate_z": z,
        "alerts": alerts,
    }
