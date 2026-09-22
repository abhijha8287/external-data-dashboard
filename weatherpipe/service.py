"""The 'compute' step glue: turns stored rows into API payloads, with an invalidation-safe cache."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, Optional, Tuple

from . import metrics, store
from .config import Settings
from .db import ISO, iso, parse_iso, utcnow


class BadRequest(ValueError):
    pass


class NotFound(LookupError):
    pass


def parse_bound(value: Optional[str], end_of_day: bool = False) -> Optional[str]:
    """Accept 'YYYY-MM-DD' or an ISO datetime (with optional Z / seconds); return canonical UTC string."""
    if not value:
        return None
    v = value.strip().rstrip("Z")
    for fmt, date_only in (("%Y-%m-%dT%H:%M:%S", False), ("%Y-%m-%dT%H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(v, fmt)
        except ValueError:
            continue
        if date_only and end_of_day:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.strftime(ISO)
    raise BadRequest("invalid date %r (use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS])" % value)


class MetricsCache:
    """Caches computed metrics keyed by (entity, params, data_version).

    No TTL needed: the key includes MAX(snapshot id) for the entity, so any insert (a new pull or
    a backfill) changes the key and the stale entry is simply never read again. Correct by
    construction; the price is one indexed MAX() query per request instead of a full recompute.
    """

    def __init__(self, max_entries: int = 256):
        self._d: Dict[Tuple, dict] = {}
        self._lock = threading.Lock()
        self.hits = self.misses = 0
        self._max = max_entries

    def get_or_compute(self, key, fn):
        with self._lock:
            if key in self._d:
                self.hits += 1
                return self._d[key], True
        value = fn()
        with self._lock:
            self.misses += 1
            if len(self._d) >= self._max:
                self._d.clear()
            self._d[key] = value
        return value, False


def _row_json(r) -> dict:
    return {
        "observed_at": r["observed_at"], "fetched_at": r["fetched_at"],
        "temperature_c": r["temperature_c"], "wind_kmh": r["wind_kmh"],
        "humidity_pct": r["humidity_pct"], "source": r["source"],
    }


def _metrics_json(m: dict) -> dict:
    m = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
    for k in ("min", "max"):
        m[k] = {"value": m[k]["value"], "at": iso(m[k]["at"])}
    return m


def compute_status(conn, settings: Settings, entity_id: str, latest_row, now=None) -> dict:
    """What the dashboard should tell the user about pipeline health, in plain terms."""
    now = now or utcnow()
    last_ok = store.last_run(conn, entity_id, only_success=True)
    last_any = store.last_run(conn, entity_id)
    age = int((now - parse_iso(latest_row["observed_at"])).total_seconds()) if latest_row else None
    since_ok = int((now - parse_iso(last_ok["started_at"])).total_seconds()) if last_ok else None
    base = {
        "last_success_at": last_ok["started_at"] if last_ok else None,
        "last_attempt_at": last_any["started_at"] if last_any else None,
        "last_error": last_any["error"] if last_any and last_any["status"] == "error" else None,
        "data_age_s": age,
    }
    if latest_row is None:
        state = "no_data"
        msg = "No data yet. Run `python -m weatherpipe pull` (or `backfill`)."
        if last_any and last_any["status"] == "error":
            state, msg = "failing", "The first data pull failed: %s" % last_any["error"]
    elif last_any and last_any["status"] == "error":
        state = "failing"
        msg = "Latest data pull failed (%s). Showing last known good data." % last_any["error"]
    elif since_ok is None or since_ok > settings.stale_after_s:
        state = "stale"
        msg = "No successful pull in %s. The pipeline may not be running." % _human(since_ok)
    else:
        state, msg = "ok", "Up to date."
    base.update(state=state, message=msg)
    return base


def _human(seconds: Optional[int]) -> str:
    if seconds is None:
        return "a long time"
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    return "%.1f h" % (seconds / 3600)


def get_latest(conn, settings: Settings, cache: MetricsCache, entity_id: str) -> dict:
    if not store.entity_exists(conn, entity_id):
        raise NotFound("unknown entity %r" % entity_id)
    # Fetch enough history for the rolling window, z-score baseline (window+2) and the stat horizon.
    key = (entity_id, store.data_version(conn, entity_id), settings.rolling_window,
           settings.stat_window_hours, settings.spike_threshold_c, settings.z_threshold)

    def compute():
        stat_rows = int(settings.stat_window_hours * 4) + 2  # 15-min cadence
        rows = store.latest_rows(conn, entity_id, max(settings.rolling_window + 2, stat_rows))
        if not rows:
            return {"latest": None, "metrics": None, "alerts": []}
        m = metrics.compute(
            store.to_points(rows), window=settings.rolling_window, stat_hours=settings.stat_window_hours,
            spike_abs=settings.spike_threshold_c, z_threshold=settings.z_threshold,
        )
        alerts = m.pop("alerts")
        return {"latest": _row_json(rows[-1]), "metrics": _metrics_json(m), "alerts": alerts}

    body, cached = cache.get_or_compute(key, compute)
    latest_row = {"observed_at": body["latest"]["observed_at"]} if body["latest"] else None
    ent = next(e for e in store.list_entities(conn) if e["id"] == entity_id)
    return {
        "entity": {"id": ent["id"], "name": ent["name"]},
        **body,
        "status": compute_status(conn, settings, entity_id, latest_row),  # never cached: depends on "now"
        "cached": cached,
    }


def get_history(conn, settings: Settings, entity_id: str, start: Optional[str], end: Optional[str],
                limit: int, window: Optional[int] = None) -> dict:
    if not store.entity_exists(conn, entity_id):
        raise NotFound("unknown entity %r" % entity_id)
    if limit < 1 or limit > 5000:
        raise BadRequest("limit must be between 1 and 5000")
    window = window or settings.rolling_window
    if window < 1 or window > 500:
        raise BadRequest("window must be between 1 and 500")
    lo, hi = parse_bound(start), parse_bound(end, end_of_day=True)
    if lo and hi and lo > hi:
        raise BadRequest("'from' must not be after 'to'")

    rows = store.rows_between(conn, entity_id, lo, hi)
    # Warm up the rolling mean with the (window-1) points before `from`, so the first visible
    # point isn't computed from a truncated window.
    warm = store.rows_before(conn, entity_id, rows[0]["observed_at"], window - 1) if rows and window > 1 else []
    roll = metrics.rolling_series(store.to_points(warm + list(rows)), window)[len(warm):]
    pairs = list(zip(rows, roll))
    total = len(pairs)
    pairs = pairs[-limit:]
    return {
        "entity": entity_id, "from": lo, "to": hi, "rolling_window": window,
        "count": len(pairs), "total_in_range": total, "truncated": total > len(pairs),
        "points": [dict(_row_json(r), rolling_avg=round(a, 4)) for r, a in pairs],
    }
