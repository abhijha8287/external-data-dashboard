"""Metric maths. Expected values are worked out by hand in the comments, not copied from the code."""
import statistics
import unittest
from datetime import datetime, timedelta, timezone

from weatherpipe import metrics as m
from weatherpipe.metrics import Point

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def series(values, step_min=15):
    return [Point(T0 + timedelta(minutes=step_min * i), v) for i, v in enumerate(values)]


class PctChange(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(m.pct_change(20, 22), 10.0)     # +2 on 20 = +10%
        self.assertAlmostEqual(m.pct_change(20, 15), -25.0)    # -5 on 20 = -25%

    def test_zero_previous_is_undefined_not_inf_or_crash(self):
        self.assertIsNone(m.pct_change(0, 3))

    def test_negative_previous_keeps_direction(self):
        # -10 -> -5 is a warming of +5 C. Naive (cur-prev)/prev would give -50% (wrong sign).
        self.assertAlmostEqual(m.pct_change(-10, -5), 50.0)
        self.assertAlmostEqual(m.pct_change(-10, -15), -50.0)


class Compute(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(m.compute([]))

    def test_single_point(self):
        r = m.compute(series([10.0]))
        self.assertIsNone(r["change_abs"])
        self.assertIsNone(r["change_pct"])
        self.assertIsNone(r["stdev"])
        self.assertIsNone(r["slope_per_hour"])
        self.assertEqual(r["rolling_avg"], 10.0)
        self.assertEqual((r["min"]["value"], r["max"]["value"]), (10.0, 10.0))

    def test_change_uses_last_two_points_only(self):
        r = m.compute(series([1, 100, 10, 12]))
        self.assertEqual(r["previous"], 10)
        self.assertAlmostEqual(r["change_abs"], 2)
        self.assertAlmostEqual(r["change_pct"], 20.0)

    def test_rolling_avg_is_last_n_including_latest(self):
        # window=3 over [10,20,30,40,50] -> mean(30,40,50) = 40 (not mean of all = 30, not 20,30,40 = 30)
        r = m.compute(series([10, 20, 30, 40, 50]), window=3)
        self.assertAlmostEqual(r["rolling_avg"], 40.0)
        self.assertEqual(r["rolling_n_used"], 3)

    def test_rolling_avg_with_fewer_points_than_window(self):
        r = m.compute(series([10, 20]), window=12)
        self.assertAlmostEqual(r["rolling_avg"], 15.0)
        self.assertEqual(r["rolling_n_used"], 2)

    def test_min_max_are_time_windowed_and_report_when(self):
        # 0h:-5(old, out of 1h window) 0.25h:...; latest at 2h. stat_hours=1 keeps t>=1h.
        pts = series([-5, 0, 0, 0, 7, 3, 9, 4, 6], step_min=15)  # times 0..2h
        r = m.compute(pts, stat_hours=1.0)
        # in-window points are index 4..8 -> values 7,3,9,4,6
        self.assertEqual(r["min"]["value"], 3)
        self.assertEqual(r["max"]["value"], 9)
        self.assertEqual(r["max"]["at"], pts[6].t)
        self.assertEqual(r["stat_n"], 5)

    def test_min_max_boundary_is_inclusive(self):
        pts = series([100, 1, 2, 3, 4], step_min=60)  # latest at 4h; stat_hours=4 includes t=0
        self.assertEqual(m.compute(pts, stat_hours=4.0)["max"]["value"], 100)
        self.assertEqual(m.compute(pts, stat_hours=3.9)["max"]["value"], 4)

    def test_stdev_matches_stdlib_sample_stdev(self):
        vals = [3.1, 4.7, 2.2, 8.0, 5.5, 6.1]
        r = m.compute(series(vals), window=6)
        self.assertAlmostEqual(r["stdev"], statistics.stdev(vals))
        self.assertAlmostEqual(m.sample_stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13809, places=4)

    def test_constant_series_has_zero_stdev_zero_slope(self):
        r = m.compute(series([5.0] * 10))
        self.assertEqual(r["stdev"], 0.0)
        self.assertAlmostEqual(r["slope_per_hour"], 0.0)


class Slope(unittest.TestCase):
    def test_exact_line(self):
        # +1 C every 15 min = +4 C/hour
        self.assertAlmostEqual(m.slope_per_hour(series([0, 1, 2, 3, 4])), 4.0)

    def test_respects_irregular_spacing(self):
        pts = [Point(T0, 0.0), Point(T0 + timedelta(hours=1), 2.0), Point(T0 + timedelta(hours=3), 6.0)]
        self.assertAlmostEqual(m.slope_per_hour(pts), 2.0)  # 2 C/h throughout

    def test_negative(self):
        self.assertAlmostEqual(m.slope_per_hour(series([10, 9, 8])), -4.0)

    def test_needs_two_points(self):
        self.assertIsNone(m.slope_per_hour(series([1.0])))


class RollingSeries(unittest.TestCase):
    def test_values(self):
        self.assertEqual(m.rolling_series(series([2, 4, 6, 8]), 2), [2, 3, 5, 7])

    def test_window_one_is_identity(self):
        self.assertEqual(m.rolling_series(series([2, 4, 6]), 1), [2, 4, 6])


class Anomaly(unittest.TestCase):
    def test_smooth_warming_trend_is_not_anomalous(self):
        # Steady +1 C/15min ramp (like sunrise). Level z-score would be huge; rate z-score is 0.
        r = m.compute(series(list(range(20))), window=12)
        self.assertAlmostEqual(r["rate_z"], 0.0)
        self.assertFalse([a for a in r["alerts"] if a["code"] == "anomaly"])

    def test_abrupt_jump_is_flagged(self):
        vals = [10, 10.2, 10.1, 10.3, 10.2, 10.1, 10.2, 10.3, 16.0]
        r = m.compute(series(vals), window=8, spike_abs=99)
        self.assertGreaterEqual(r["rate_z"], 3.0)
        self.assertEqual([a["code"] for a in r["alerts"]], ["anomaly"])

    def test_too_little_history_gives_none_not_false_alarm(self):
        self.assertIsNone(m.compute(series([1, 2, 3, 40]))["rate_z"])

    def test_sd_floor_prevents_flat_series_false_positive(self):
        vals = [10.0] * 8 + [10.2]   # 0.2 C wobble on a perfectly flat series
        self.assertLess(abs(m.compute(series(vals), window=8)["rate_z"]), 3.0)


class SpikeAlert(unittest.TestCase):
    def test_threshold_is_inclusive_and_symmetric(self):
        self.assertEqual(m.compute(series([10, 12]), spike_abs=2.0)["alerts"][0]["code"], "spike")
        self.assertEqual(m.compute(series([12, 10]), spike_abs=2.0)["alerts"][0]["code"], "spike")
        self.assertEqual(m.compute(series([10, 11.9]), spike_abs=2.0)["alerts"], [])


if __name__ == "__main__":
    unittest.main()
