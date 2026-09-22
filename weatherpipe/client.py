"""Open-Meteo HTTP client. The only module that touches the network.

Responsibilities: timeouts, bounded retries with exponential backoff (transient errors only),
and *validating* the payload so garbage never reaches the database.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from .config import Settings

VARS = "temperature_2m,wind_speed_10m,relative_humidity_2m"
TEMP_RANGE_C = (-90.0, 60.0)  # beyond any recorded surface temperature => bad data


class PullError(Exception):
    """Any failure to obtain a valid reading. `attempts` = HTTP attempts made."""

    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class Reading:
    observed_at: str  # UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'
    temperature_c: float
    wind_kmh: Optional[float]
    humidity_pct: Optional[float]


def _norm_time(s: str) -> str:
    # We request timezone=UTC, so "2026-09-21T19:15" is UTC.
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        raise PullError("bad timestamp in response: %r" % (s,))
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def _num(v, name: str, required: bool = False) -> Optional[float]:
    if v is None:
        if required:
            raise PullError("missing %s in response" % name)
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise PullError("non-numeric %s: %r" % (name, v))
    return float(v)


def _check_temp(t: float) -> float:
    if not (TEMP_RANGE_C[0] <= t <= TEMP_RANGE_C[1]):
        raise PullError("implausible temperature %.1f C" % t)
    return t


def parse_current(payload: dict) -> Reading:
    try:
        cur = payload["current"]
        return Reading(
            observed_at=_norm_time(cur["time"]),
            temperature_c=_check_temp(_num(cur.get("temperature_2m"), "temperature_2m", True)),
            wind_kmh=_num(cur.get("wind_speed_10m"), "wind_speed_10m"),
            humidity_pct=_num(cur.get("relative_humidity_2m"), "relative_humidity_2m"),
        )
    except (KeyError, TypeError, AttributeError):
        raise PullError("unexpected response shape (no 'current' block)")


def parse_history(payload: dict, now: Optional[datetime] = None) -> List[Reading]:
    """15-minute series. The API pads the day with *forecast* slots; drop anything in the future
    and any null slots so we only ever store observations."""
    now = now or datetime.now(timezone.utc)
    try:
        m = payload["minutely_15"]
        times, temps = m["time"], m["temperature_2m"]
        winds = m.get("wind_speed_10m") or [None] * len(times)
        hums = m.get("relative_humidity_2m") or [None] * len(times)
    except (KeyError, TypeError, AttributeError):
        raise PullError("unexpected response shape (no 'minutely_15' block)")
    out = []
    for t, temp, w, h in zip(times, temps, winds, hums):
        obs = _norm_time(t)
        if temp is None or datetime.strptime(obs, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) > now:
            continue
        out.append(Reading(obs, _check_temp(_num(temp, "temperature_2m", True)), _num(w, "wind"), _num(h, "humidity")))
    if not out:
        raise PullError("history response contained no usable observations")
    return out


def _get_json(url: str, s: Settings, sleep: Callable[[float], None]) -> Tuple[dict, int]:
    """GET with retries. Retries network errors, timeouts, 429 and 5xx; fails fast on other 4xx."""
    last = "unknown error"
    attempts = max(1, s.http_retries)
    for attempt in range(1, attempts + 1):
        retryable = True
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weatherpipe/1.0"})
            with urllib.request.urlopen(req, timeout=s.http_timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8")), attempt
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            retryable = e.code == 429 or e.code >= 500
        except urllib.error.URLError as e:
            last = "network error: %s" % (e.reason,)
        except (TimeoutError, OSError) as e:  # socket timeouts surface here
            last = "network error: %s" % (e,)
        except ValueError:                     # bad JSON
            last = "response was not valid JSON"
        if not retryable:
            raise PullError(last, attempt)
        if attempt < attempts:
            sleep(s.retry_backoff_s * (2 ** (attempt - 1)))
    raise PullError("%s (gave up after %d attempts)" % (last, attempts), attempts)


def _url(s: Settings, lat: float, lon: float, extra: dict) -> str:
    q = {"latitude": lat, "longitude": lon, "timezone": "UTC"}
    q.update(extra)
    return s.base_url + "?" + urllib.parse.urlencode(q, safe=",")


def fetch_current(s: Settings, lat: float, lon: float, sleep=time.sleep) -> Tuple[Reading, int]:
    payload, attempts = _get_json(_url(s, lat, lon, {"current": VARS}), s, sleep)
    try:
        return parse_current(payload), attempts
    except PullError as e:
        e.attempts = attempts
        raise


def fetch_history(s: Settings, lat: float, lon: float, days: int, sleep=time.sleep) -> Tuple[List[Reading], int]:
    extra = {"minutely_15": VARS, "past_days": days, "forecast_days": 1}
    payload, attempts = _get_json(_url(s, lat, lon, extra), s, sleep)
    try:
        return parse_history(payload), attempts
    except PullError as e:
        e.attempts = attempts
        raise
