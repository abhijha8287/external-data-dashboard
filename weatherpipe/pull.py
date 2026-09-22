"""The 'pull' step: fetch -> validate -> store -> log. Contract: never raises for upstream problems.

One city failing must not affect the others, and a failure is recorded (not swallowed) so the
dashboard can say *what* went wrong instead of silently showing old numbers.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, Optional, Tuple

from . import client, store
from .config import CITIES, Settings
from .db import utcnow

log = logging.getLogger("weatherpipe.pull")


def _run(conn, entity_id: str, kind: str, work: Callable[[], Tuple[int, int]]) -> Dict:
    started = utcnow()
    t0 = time.monotonic()
    status, rows, attempts, error = "error", 0, 1, None
    try:
        rows, attempts = work()
        status = "ok" if rows > 0 else "unchanged"
    except client.PullError as e:
        attempts, error = e.attempts, str(e)
    except Exception as e:  # defensive: a bug or DB hiccup must not kill the scheduler loop
        error = "internal error: %s: %s" % (type(e).__name__, e)
        log.exception("unexpected failure pulling %s", entity_id)
    ms = int((time.monotonic() - t0) * 1000)
    try:
        store.log_run(conn, entity_id, kind, started, ms, status, rows, attempts, error)
    except Exception:
        log.exception("could not record pull_run for %s", entity_id)
    if error:
        log.warning("%s pull FAILED for %s: %s", kind, entity_id, error)
    else:
        log.info("%s pull %s for %s (+%d rows, %d ms)", kind, status, entity_id, rows, ms)
    return {"entity": entity_id, "status": status, "rows_inserted": rows, "attempts": attempts, "error": error, "duration_ms": ms}


def pull_entity(conn, settings: Settings, entity_id: str, sleep=time.sleep) -> Dict:
    _, lat, lon = CITIES[entity_id]

    def work():
        reading, attempts = client.fetch_current(settings, lat, lon, sleep)
        return store.insert_readings(conn, entity_id, [reading], "live"), attempts

    return _run(conn, entity_id, "live", work)


def backfill_entity(conn, settings: Settings, entity_id: str, days: int, sleep=time.sleep) -> Dict:
    _, lat, lon = CITIES[entity_id]

    def work():
        readings, attempts = client.fetch_history(settings, lat, lon, days, sleep)
        return store.insert_readings(conn, entity_id, readings, "backfill"), attempts

    return _run(conn, entity_id, "backfill", work)


def pull_all(conn, settings: Settings, only: Optional[str] = None, sleep=time.sleep):
    ids = [only] if only else list(CITIES)
    return [pull_entity(conn, settings, i, sleep) for i in ids]
