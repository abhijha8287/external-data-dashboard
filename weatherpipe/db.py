"""SQLite schema and connection helper.

Design notes
- `snapshots` is append-only history. UNIQUE(entity_id, observed_at) makes pulls idempotent:
  Open-Meteo refreshes every 15 min, so pulling more often just re-sees the same observation.
  We key on the *observation* time (what the upstream says), not our fetch time.
- `fetched_at` is kept separately so we can measure staleness/latency of our own pipeline.
- `pull_runs` is an operational log, separate from data, so a failed pull (which produces no
  snapshot) is still visible and the dashboard can explain "why is my data old?".
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import CITIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    latitude  REAL NOT NULL,
    longitude REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    observed_at   TEXT NOT NULL,          -- upstream observation time, UTC ISO-8601 'Z'
    fetched_at    TEXT NOT NULL,          -- when *we* stored it
    temperature_c REAL NOT NULL,
    wind_kmh      REAL,
    humidity_pct  REAL,
    source        TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live', 'backfill')),
    UNIQUE (entity_id, observed_at)       -- also serves as the (entity, time) range index
);

CREATE TABLE IF NOT EXISTS pull_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id     TEXT NOT NULL REFERENCES entities(id),
    kind          TEXT NOT NULL CHECK (kind IN ('live', 'backfill')),
    started_at    TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'unchanged', 'error')),
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_entity_time ON pull_runs(entity_id, id DESC);
"""

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(ISO)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # readers (dashboard) never block the writer (pull)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO entities (id, name, latitude, longitude) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, latitude=excluded.latitude, "
        "longitude=excluded.longitude",
        [(cid, n, la, lo) for cid, (n, la, lo) in CITIES.items()],
    )
    conn.commit()
