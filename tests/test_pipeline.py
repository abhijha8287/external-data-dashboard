import unittest
from datetime import datetime, timezone

from tests.helpers import FakeUpstream, current_payload, temp_settings
from weatherpipe import client, pull, store
from weatherpipe.db import connect, init_db


def setup(script, **kw):
    up = FakeUpstream(script)
    s = temp_settings(up.url, **kw)
    conn = connect(s.db_path)
    init_db(conn)
    return up, s, conn


class PullHappyPath(unittest.TestCase):
    def test_pull_stores_snapshot_and_logs_run(self):
        up, s, conn = setup([current_payload(temp=21.5)])
        self.addCleanup(up.close)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual((r["status"], r["rows_inserted"]), ("ok", 1))
        row = conn.execute("SELECT * FROM snapshots").fetchone()
        self.assertEqual((row["temperature_c"], row["observed_at"], row["source"]), (21.5, "2026-09-21T19:15:00Z", "live"))
        self.assertEqual(store.last_run(conn, "london")["status"], "ok")

    def test_history_accumulates_across_pulls(self):
        up, s, conn = setup([current_payload("2026-09-21T19:15", 20), current_payload("2026-09-21T19:30", 21),
                             current_payload("2026-09-21T19:45", 22)])
        self.addCleanup(up.close)
        for _ in range(3):
            pull.pull_entity(conn, s, "london")
        vals = [r["temperature_c"] for r in store.latest_rows(conn, "london", 10)]
        self.assertEqual(vals, [20, 21, 22])   # append-only, not overwritten

    def test_repeat_of_same_observation_is_idempotent(self):
        up, s, conn = setup([current_payload()])
        self.addCleanup(up.close)
        a, b = pull.pull_entity(conn, s, "london"), pull.pull_entity(conn, s, "london")
        self.assertEqual((a["status"], b["status"]), ("ok", "unchanged"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 1)


class PullFailures(unittest.TestCase):
    def test_500_is_retried_then_succeeds(self):
        up, s, conn = setup([500, 500, current_payload()])
        self.addCleanup(up.close)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual((r["status"], r["attempts"], up.calls), ("ok", 3, 3))

    def test_persistent_failure_does_not_raise_and_is_logged(self):
        up, s, conn = setup([503])
        self.addCleanup(up.close)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual(r["status"], "error")
        self.assertIn("503", r["error"])
        self.assertEqual(up.calls, 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0)
        self.assertEqual(store.last_run(conn, "london")["status"], "error")

    def test_4xx_is_not_retried(self):
        up, s, conn = setup([404])
        self.addCleanup(up.close)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual((r["status"], up.calls), ("error", 1))

    def test_slow_upstream_times_out(self):
        up, s, conn = setup([("sleep", 1.0, current_payload())], http_timeout_s=0.3, http_retries=2)
        self.addCleanup(up.close)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["attempts"], 2)

    def test_connection_refused(self):
        s = temp_settings("http://127.0.0.1:1/", http_retries=2)
        conn = connect(s.db_path); init_db(conn)
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual(r["status"], "error")
        self.assertIn("network error", r["error"])

    def test_garbage_body_rejected(self):
        up, s, conn = setup(["garbage"])
        self.addCleanup(up.close)
        self.assertEqual(pull.pull_entity(conn, s, "london")["status"], "error")

    def test_bad_data_never_reaches_the_database(self):
        for bad in ({"current": {"time": "2026-09-21T19:15", "temperature_2m": None}},
                    {"current": {"time": "2026-09-21T19:15", "temperature_2m": "hot"}},
                    {"current": {"time": "2026-09-21T19:15", "temperature_2m": 999}},   # implausible
                    {"current": {"time": "yesterday", "temperature_2m": 20}},
                    {"unexpected": True}):
            up, s, conn = setup([bad])
            r = pull.pull_entity(conn, s, "london")
            up.close()
            self.assertEqual(r["status"], "error", bad)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 0, bad)

    def test_one_failing_city_does_not_stop_the_others(self):
        up, s, conn = setup([503, 503, 503, current_payload(), current_payload("2026-09-21T19:30")])
        self.addCleanup(up.close)
        res = pull.pull_all(conn, s, sleep=lambda x: None)
        # first city burns its 3 attempts on 503s; remaining cities all still run
        self.assertEqual(len(res), 5)
        self.assertEqual(res[0]["status"], "error")
        self.assertTrue(all(r["status"] in ("ok", "unchanged") for r in res[1:]))

    def test_internal_error_is_contained(self):
        up, s, conn = setup([current_payload()])
        self.addCleanup(up.close)
        conn.execute("DROP TABLE snapshots")  # simulate DB corruption
        r = pull.pull_entity(conn, s, "london")
        self.assertEqual(r["status"], "error")
        self.assertIn("internal error", r["error"])


class Backfill(unittest.TestCase):
    def test_future_forecast_slots_and_nulls_are_dropped(self):
        now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
        payload = {"minutely_15": {
            "time": ["2026-09-21T11:30", "2026-09-21T11:45", "2026-09-21T12:00", "2026-09-21T12:15", "2026-09-21T12:30"],
            "temperature_2m": [10.0, None, 11.0, 12.0, 13.0],
            "wind_speed_10m": [1, 2, 3, 4, 5], "relative_humidity_2m": [50, 50, 50, 50, 50]}}
        got = client.parse_history(payload, now=now)
        self.assertEqual([r.observed_at for r in got], ["2026-09-21T11:30:00Z", "2026-09-21T12:00:00Z"])

    def test_backfill_then_live_pull_dedupes(self):
        up, s, conn = setup([current_payload("2026-09-21T19:15", 20)])
        self.addCleanup(up.close)
        n = store.insert_readings(conn, "london", [client.Reading("2026-09-21T19:15:00Z", 20.0, 1, 1)], "backfill")
        self.assertEqual(n, 1)
        self.assertEqual(pull.pull_entity(conn, s, "london")["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
