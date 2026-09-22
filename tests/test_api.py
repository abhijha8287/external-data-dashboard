import json
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from tests.helpers import temp_settings
from weatherpipe import server, store
from weatherpipe.client import Reading
from weatherpipe.db import connect, init_db, iso, utcnow


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.s = temp_settings(rolling_window=3)
        self.conn = connect(self.s.db_path)
        init_db(self.conn)
        self.srv = server.create_server(self.s, port=0)
        server.serve_in_thread(self.srv)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def seed(self, values, entity="london", step=15, end=None):
        end = (end or utcnow()).replace(second=0)
        rs = [Reading(iso(end - timedelta(minutes=step * (len(values) - 1 - i))), v, 3.0, 40.0) for i, v in enumerate(values)]
        store.insert_readings(self.conn, entity, rs, "live")
        return rs

    def test_latest_shape_and_metrics(self):
        self.seed([10, 11, 12, 15])
        code, d = get(self.base, "/latest?entity=london")
        self.assertEqual(code, 200)
        self.assertEqual(d["latest"]["temperature_c"], 15)
        self.assertAlmostEqual(d["metrics"]["change_abs"], 3)
        self.assertAlmostEqual(d["metrics"]["change_pct"], 25.0)
        self.assertAlmostEqual(d["metrics"]["rolling_avg"], (11 + 12 + 15) / 3, places=3)   # window=3; API rounds to 4dp
        self.assertEqual((d["metrics"]["min"]["value"], d["metrics"]["max"]["value"]), (10, 15))
        self.assertEqual([a["code"] for a in d["alerts"]], ["spike"])          # +3 >= 2.0
        self.assertEqual(d["status"]["state"], "ok" if d["status"]["last_success_at"] else "stale")

    def test_no_data_state(self):
        code, d = get(self.base, "/latest?entity=tokyo")
        self.assertEqual(code, 200)
        self.assertIsNone(d["latest"])
        self.assertEqual(d["status"]["state"], "no_data")

    def test_failed_pull_serves_last_known_good_with_failing_state(self):
        self.seed([10, 11])
        store.log_run(self.conn, "london", "live", utcnow(), 5, "ok", 1, 1)
        store.log_run(self.conn, "london", "live", utcnow(), 900, "error", 0, 3, "HTTP 503 (gave up after 3 attempts)")
        code, d = get(self.base, "/latest?entity=london")
        self.assertEqual(code, 200)
        self.assertEqual(d["latest"]["temperature_c"], 11)            # last known good
        self.assertEqual(d["status"]["state"], "failing")
        self.assertIn("503", d["status"]["last_error"])

    def test_stale_state_when_no_recent_success(self):
        self.seed([10, 11])
        store.log_run(self.conn, "london", "live", utcnow() - timedelta(hours=3), 5, "ok", 1, 1)
        self.assertEqual(get(self.base, "/latest")[1]["status"]["state"], "stale")

    def test_recovery_clears_failing_state(self):
        self.seed([10, 11])
        store.log_run(self.conn, "london", "live", utcnow(), 5, "error", 0, 3, "boom")
        store.log_run(self.conn, "london", "live", utcnow(), 5, "ok", 1, 1)
        self.assertEqual(get(self.base, "/latest")[1]["status"]["state"], "ok")

    def test_cache_hit_then_invalidated_by_new_data(self):
        self.seed([10, 11])
        self.assertFalse(get(self.base, "/latest")[1]["cached"])
        self.assertTrue(get(self.base, "/latest")[1]["cached"])
        store.insert_readings(self.conn, "london", [Reading(iso(utcnow() + timedelta(minutes=15)), 30.0, 1, 1)], "live")
        d = get(self.base, "/latest")[1]
        self.assertFalse(d["cached"])
        self.assertEqual(d["latest"]["temperature_c"], 30.0)         # not the stale cached value

    def test_history_ordering_and_range_filter(self):
        rs = self.seed([1, 2, 3, 4, 5, 6])
        _, all_ = get(self.base, "/history?entity=london")
        self.assertEqual([p["temperature_c"] for p in all_["points"]], [1, 2, 3, 4, 5, 6])
        lo, hi = rs[1].observed_at[:19], rs[3].observed_at[:19]
        _, sub = get(self.base, "/history?entity=london&from=%s&to=%s" % (lo, hi))
        self.assertEqual([p["temperature_c"] for p in sub["points"]], [2, 3, 4])   # inclusive both ends

    def test_history_rolling_avg_warmed_up_before_from(self):
        rs = self.seed([10, 20, 30, 40])
        _, d = get(self.base, "/history?entity=london&from=%s" % rs[2].observed_at[:19])
        # first visible point (30) must use window [10,20,30] -> 20, not a truncated [30] -> 30
        self.assertAlmostEqual(d["points"][0]["rolling_avg"], 20.0)
        self.assertAlmostEqual(d["points"][1]["rolling_avg"], 30.0)

    def test_history_date_only_to_is_end_of_day(self):
        end = utcnow().replace(hour=12, minute=0)
        self.seed([1, 2], end=end)
        day = end.strftime("%Y-%m-%d")
        _, d = get(self.base, "/history?entity=london&from=%s&to=%s" % (day, day))
        self.assertEqual(d["count"], 2)

    def test_history_limit_returns_latest_points(self):
        self.seed([1, 2, 3, 4, 5])
        _, d = get(self.base, "/history?entity=london&limit=2")
        self.assertEqual([p["temperature_c"] for p in d["points"]], [4, 5])
        self.assertTrue(d["truncated"])

    def test_entities_are_isolated(self):
        self.seed([1, 2], "london")
        self.seed([50, 60], "tokyo")
        self.assertEqual(get(self.base, "/latest?entity=tokyo")[1]["latest"]["temperature_c"], 60)
        self.assertEqual(get(self.base, "/latest?entity=london")[1]["latest"]["temperature_c"], 2)

    def test_errors_are_clean_json(self):
        self.assertEqual(get(self.base, "/latest?entity=atlantis")[0], 404)
        self.assertEqual(get(self.base, "/history?entity=london&from=garbage")[0], 400)
        self.assertEqual(get(self.base, "/history?entity=london&limit=abc")[0], 400)
        self.assertEqual(get(self.base, "/history?entity=london&limit=0")[0], 400)
        self.assertEqual(get(self.base, "/history?entity=london&from=2026-02-01&to=2026-01-01")[0], 400)
        self.assertEqual(get(self.base, "/nope")[0], 404)

    def test_dashboard_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"<svg", r.read())


if __name__ == "__main__":
    unittest.main()
