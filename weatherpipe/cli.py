"""Command line entry point: python -m weatherpipe <command>"""
from __future__ import annotations

import argparse
import logging
import threading

from . import pull, server, store
from .config import CITIES, Settings
from .db import connect, init_db


def _scheduler(settings: Settings, every: int, stop: threading.Event) -> None:
    """Simple in-process scheduler. Each cycle is fully guarded: it can log failures but never die."""
    log = logging.getLogger("weatherpipe.scheduler")
    while not stop.is_set():
        try:
            conn = connect(settings.db_path)
            try:
                pull.pull_all(conn, settings, sleep=lambda s: stop.wait(s))
            finally:
                conn.close()
        except Exception:
            log.exception("scheduler cycle failed; will retry next interval")
        stop.wait(every)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weatherpipe", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database and entities")
    pl = sub.add_parser("pull", help="pull the current reading once for every city (or one)")
    pl.add_argument("--city", choices=list(CITIES))
    bf = sub.add_parser("backfill", help="load real recent history (15-min resolution) to seed the DB")
    bf.add_argument("--days", type=int, default=2, help="1-92 days (default 2)")
    bf.add_argument("--city", choices=list(CITIES))
    rn = sub.add_parser("run", help="pull on a loop (a tiny scheduler)")
    rn.add_argument("--every", type=int, default=900, help="seconds between pulls (default 900 = 15 min)")
    sv = sub.add_parser("serve", help="serve API + dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--pull-every", type=int, default=0, metavar="SEC", help="also run the scheduler in-process")
    sub.add_parser("status", help="show the latest pull runs")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    settings = Settings()

    if args.cmd == "serve":
        srv = server.create_server(settings, args.host, args.port)
        stop = threading.Event()
        if args.pull_every:
            threading.Thread(target=_scheduler, args=(settings, args.pull_every, stop), daemon=True).start()
        print("Dashboard: http://%s:%d/" % (args.host, args.port))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            srv.server_close()
        return 0

    conn = connect(settings.db_path)
    init_db(conn)
    try:
        if args.cmd == "init":
            print("Initialised %s" % settings.db_path)
        elif args.cmd == "pull":
            results = pull.pull_all(conn, settings, args.city)
            for r in results:
                print("%-9s %-9s %s" % (r["entity"], r["status"], r["error"] or "+%d rows" % r["rows_inserted"]))
            return 1 if all(r["status"] == "error" for r in results) else 0
        elif args.cmd == "backfill":
            if not 1 <= args.days <= 92:
                p.error("--days must be between 1 and 92")
            for cid in ([args.city] if args.city else CITIES):
                r = pull.backfill_entity(conn, settings, cid, args.days)
                print("%-9s %-9s %s" % (cid, r["status"], r["error"] or "+%d rows" % r["rows_inserted"]))
        elif args.cmd == "run":
            stop = threading.Event()
            print("Pulling every %ds. Ctrl+C to stop." % args.every)
            try:
                _scheduler(settings, args.every, stop)
            except KeyboardInterrupt:
                stop.set()
        elif args.cmd == "status":
            for r in store.recent_runs(conn, None, 20):
                print("%s %-9s %-8s %-9s %4dms %s" % (r["started_at"], r["entity_id"], r["kind"], r["status"], r["duration_ms"], r["error"] or ""))
    finally:
        conn.close()
    return 0
