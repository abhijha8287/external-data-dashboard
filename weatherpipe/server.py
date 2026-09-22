"""HTTP API + static dashboard, stdlib only (http.server)."""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import service, store
from .config import Settings
from .db import connect, init_db

log = logging.getLogger("weatherpipe.http")
STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")


def make_handler(settings: Settings, cache: service.MetricsCache):
    class Handler(BaseHTTPRequestHandler):
        server_version = "weatherpipe/1.0"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    with open(STATIC, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                conn = connect(settings.db_path)
                try:
                    return self._route(conn, url.path, q)
                finally:
                    conn.close()
            except service.BadRequest as e:
                self._json(400, {"error": str(e)})
            except service.NotFound as e:
                self._json(404, {"error": str(e)})
            except Exception:
                log.exception("unhandled error for %s", self.path)
                self._json(500, {"error": "internal server error"})

        def _route(self, conn, path, q):
            if path == "/entities":
                return self._json(200, {"entities": store.list_entities(conn)})
            if path == "/latest":
                return self._json(200, service.get_latest(conn, settings, cache, q.get("entity", "london")))
            if path == "/history":
                try:
                    limit = int(q.get("limit", 1000))
                    window = int(q["window"]) if "window" in q else None
                except ValueError:
                    raise service.BadRequest("limit and window must be integers")
                return self._json(200, service.get_history(
                    conn, settings, q.get("entity", "london"), q.get("from"), q.get("to"), limit, window))
            if path == "/runs":
                return self._json(200, {"runs": store.recent_runs(conn, q.get("entity"), 20)})
            if path == "/health":
                return self._json(200, {"ok": True, "cache": {"hits": cache.hits, "misses": cache.misses}})
            self._json(404, {"error": "not found"})

    return Handler


def create_server(settings: Settings, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    conn = connect(settings.db_path)
    init_db(conn)
    conn.close()
    srv = ThreadingHTTPServer((host, port), make_handler(settings, service.MetricsCache()))
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
    t.start()
    return t
