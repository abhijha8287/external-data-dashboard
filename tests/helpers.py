"""Test helpers: a fake Open-Meteo server whose behaviour tests can script."""
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer

from weatherpipe.config import Settings


class FakeUpstream:
    """`script` is a list of callables/dicts consumed per request; the last entry repeats.
    Entry kinds: dict -> 200 JSON, int -> HTTP status, ('sleep', s) -> hang then 200, 'garbage'."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                i = min(outer.calls, len(outer.script) - 1)
                outer.calls += 1
                item = outer.script[i]
                try:
                    if isinstance(item, tuple) and item[0] == "sleep":
                        time.sleep(item[1])
                        item = item[2]
                    if isinstance(item, int):
                        self.send_response(item); self.end_headers(); return
                    body = b"<html>not json</html>" if item == "garbage" else json.dumps(item).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1/forecast" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()

    def close(self):
        self.httpd.shutdown(); self.httpd.server_close()


def current_payload(time_str="2026-09-21T19:15", temp=20.0, wind=5.0, hum=50):
    return {"current": {"time": time_str, "temperature_2m": temp, "wind_speed_10m": wind, "relative_humidity_2m": hum}}


def temp_settings(base_url="http://127.0.0.1:1/", **kw):
    d = tempfile.mkdtemp()
    defaults = dict(db_path=d + "/t.db", base_url=base_url, http_timeout_s=1.0, http_retries=3, retry_backoff_s=0.0)
    defaults.update(kw)
    return Settings(**defaults)
