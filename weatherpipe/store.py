"""Persistence layer: the only module that writes SQL for snapshots and pull runs."""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional

from .client import Reading
from .db import iso, parse_iso, utcnow
from .metrics import Point


def insert_readings(conn: sqlite3.Connection, entity_id: str, readings: Iterable[Reading], source: str) -> int:
    """Idempotent bulk insert. Returns the number of *new* rows (duplicates are ignored)."""
    fetched = iso(utcnow())
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO snapshots "
        "(entity_id, observed_at, fetched_at, temperature_c, wind_kmh, humidity_pct, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(entity_id, r.observed_at, fetched, r.temperature_c, r.wind_kmh, r.humidity_pct, source) for r in readings],
    )
    conn.commit()
    return conn.total_changes - before


def log_run(conn, entity_id, kind, started_at, duration_ms, status, rows, attempts, error=None) -> None:
    conn.execute(
        "INSERT INTO pull_runs (entity_id, kind, started_at, duration_ms, status, rows_inserted, attempts, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, kind, iso(started_at), duration_ms, status, rows, attempts, error),
    )
    conn.commit()


def entity_exists(conn, entity_id: str) -> bool:
    return conn.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is not None


def list_entities(conn) -> List[dict]:
    return [dict(r) for r in conn.execute("SELECT id, name, latitude, longitude FROM entities ORDER BY name")]


def data_version(conn, entity_id: str) -> int:
    """Cheap fingerprint of an entity's data; changes on any insert (incl. backfill). Cache key."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM snapshots WHERE entity_id=?", (entity_id,)).fetchone()[0]


def latest_rows(conn, entity_id: str, limit: int) -> List[sqlite3.Row]:
    """Most recent `limit` rows, returned oldest-first."""
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE entity_id=? ORDER BY observed_at DESC LIMIT ?", (entity_id, limit)
    ).fetchall()
    return rows[::-1]


def rows_between(conn, entity_id: str, start: Optional[str], end: Optional[str]) -> List[sqlite3.Row]:
    sql, args = "SELECT * FROM snapshots WHERE entity_id=?", [entity_id]
    if start:
        sql += " AND observed_at >= ?"
        args.append(start)
    if end:
        sql += " AND observed_at <= ?"
        args.append(end)
    return conn.execute(sql + " ORDER BY observed_at ASC", args).fetchall()


def rows_before(conn, entity_id: str, before: str, limit: int) -> List[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM snapshots WHERE entity_id=? AND observed_at < ? ORDER BY observed_at DESC LIMIT ?",
        (entity_id, before, limit),
    ).fetchall()
    return rows[::-1]


def to_points(rows: Iterable[sqlite3.Row]) -> List[Point]:
    return [Point(parse_iso(r["observed_at"]), r["temperature_c"]) for r in rows]


def recent_runs(conn, entity_id: Optional[str], limit: int = 15) -> List[dict]:
    sql, args = "SELECT * FROM pull_runs", []
    if entity_id:
        sql += " WHERE entity_id=?"
        args.append(entity_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def last_run(conn, entity_id: str, only_success: bool = False, kind: str = "live") -> Optional[dict]:
    sql = "SELECT * FROM pull_runs WHERE entity_id=? AND kind=?"
    if only_success:
        sql += " AND status IN ('ok','unchanged')"
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", (entity_id, kind)).fetchone()
    return dict(row) if row else None
