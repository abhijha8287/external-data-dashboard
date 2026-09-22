"""All tunables in one place. Every value can be overridden by an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Tracked entities: id -> (display name, latitude, longitude)
CITIES = {
    "london": ("London", 51.5072, -0.1276),
    "new-york": ("New York", 40.7128, -74.0060),
    "tokyo": ("Tokyo", 35.6762, 139.6503),
    "mumbai": ("Mumbai", 19.0760, 72.8777),
    "sydney": ("Sydney", -33.8688, 151.2093),
}


def _env(name: str, default, cast=str):
    raw = os.environ.get(name)
    return default if raw is None else cast(raw)


@dataclass(frozen=True)
class Settings:
    db_path: str = _env("WEATHERPIPE_DB", "weather.db")
    base_url: str = _env("WEATHERPIPE_BASE_URL", "https://api.open-meteo.com/v1/forecast")
    http_timeout_s: float = _env("WEATHERPIPE_TIMEOUT", 10.0, float)
    http_retries: int = _env("WEATHERPIPE_RETRIES", 3, int)
    retry_backoff_s: float = _env("WEATHERPIPE_BACKOFF", 0.5, float)
    # Metrics
    rolling_window: int = _env("WEATHERPIPE_WINDOW", 12, int)        # pulls (12 x 15min = 3h)
    stat_window_hours: float = _env("WEATHERPIPE_STAT_HOURS", 24.0, float)  # min/max horizon
    spike_threshold_c: float = _env("WEATHERPIPE_SPIKE_C", 2.0, float)  # alert: |change| >= X
    z_threshold: float = _env("WEATHERPIPE_Z", 3.0, float)
    # A pull is "stale" if the last successful pull is older than this (3 missed 15-min cycles)
    stale_after_s: float = _env("WEATHERPIPE_STALE_S", 2700.0, float)
