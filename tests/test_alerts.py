"""Unit tests for the alerts package: state diffing, dedupe, gating.
Stdlib only, no network."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from turblance_trader.alerts.state import (
    already_fired,
    diff_alerts,
    load_state,
    mark_fired,
    new_day_state,
    save_state,
    sweep_key,
)
from turblance_trader.capture.market_hours import is_market_open

ET = ZoneInfo("America/New_York")


def read(symbol, **kw):
    d = {
        "regime": "negative",
        "instinct_label": "NEUTRAL",
        "instinct_score": 5,
        "suggestion": "NO_EDGE_QUIET",
        "spot": 761.69,
        "levels": {"call_wall": 770.0, "put_wall": 745.0,
                   "zero_gamma_flip": None},
        "sweeps": [],
        "sweeps_seen": [],
    }
    d.update(kw)
    return d


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            st = new_day_state("2026-09-21")
            st["symbols"]["SPY"] = {"regime": "negative"}
            save_state(st, p)
            back = load_state(p)
            self.assertEqual(back["date"], "2026-09-21")
            self.assertEqual(back["symbols"]["SPY"]["regime"], "negative")

    def test_load_missing_and_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            self.assertEqual(load_state(p)["date"], None)
            p.write_text("{not json")
            self.assertEqual(load_state(p)["fired"], [])

    def test_new_day_resets(self):
        st = new_day_state("2026-09-22")
        self.assertEqual(st["fired"], [])
        self.assertEqual(st["symbols"], {})

    def test_mark_fired_dedupes(self):
        st = new_day_state("2026-09-21")
        mark_fired(st, "k1")
        mark_fired(st, "k1")
        self.assertTrue(already_fired(st, "k1"))
        self.assertEqual(st["fired"], ["k1"])

    def test_sweep_key_stable(self):
        sw = {"expiry": "2026-09-25", "direction": "calls", "side": "lifted",
              "strikes_hit": [760.0, 765.0, 770.0]}
        k1 = sweep_key("SPY", sw)
        k2 = sweep_key("SPY", dict(sw))
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("SPY:2026-09-25:calls:lifted:"))


class TestDiffAlerts(unittest.TestCase):
    def test_no_change_no_alerts(self):
        self.assertEqual(diff_alerts("SPY", read("SPY"), read("SPY"),
                                     "2026-09-21"), [])

    def test_regime_flip(self):
        prev = read("SPY", regime="negative")
        curr = read("SPY", regime="positive")
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "regime")
        self.assertIn("negative -> positive", alerts[0]["text"])

    def test_instinct_band_crossing(self):
        prev = read("SPY", instinct_label="NEUTRAL", instinct_score=10)
        curr = read("SPY", instinct_label="BULLISH", instinct_score=45)
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        kinds = [a["kind"] for a in alerts]
        self.assertIn("instinct", kinds)

    def test_instinct_no_band_cross_no_alert(self):
        # NEUTRAL -> LEAN BULLISH stays "mid": no alert.
        prev = read("SPY", instinct_label="NEUTRAL")
        curr = read("SPY", instinct_label="LEAN BULLISH", instinct_score=20)
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertEqual(alerts, [])

    def test_instinct_leaving_bullish_fires(self):
        prev = read("SPY", instinct_label="BULLISH", instinct_score=50)
        curr = read("SPY", instinct_label="LEAN BULLISH", instinct_score=30)
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertTrue(any(a["kind"] == "instinct" for a in alerts))

    def test_new_sweep_breadth_three(self):
        sw = {"key": "SPY:2026-09-25:calls:lifted:760,765,770",
              "direction": "calls", "side": "lifted", "n_strikes": 3}
        prev = read("SPY")
        curr = read("SPY", sweeps=[sw])
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertTrue(any(a["kind"] == "sweep" for a in alerts))

    def test_seen_sweep_no_refire(self):
        sw = {"key": "SPY:2026-09-25:calls:lifted:760,765,770",
              "direction": "calls", "side": "lifted", "n_strikes": 3}
        prev = read("SPY", sweeps_seen=[sw["key"]])
        curr = read("SPY", sweeps=[sw])
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertEqual(alerts, [])

    def test_thin_sweep_no_alert(self):
        sw = {"key": "SPY:2026-09-25:calls:lifted:760,765",
              "direction": "calls", "side": "lifted", "n_strikes": 2}
        alerts = diff_alerts("SPY", read("SPY"), read("SPY", sweeps=[sw]),
                             "2026-09-21")
        self.assertEqual(alerts, [])

    def test_suggestion_change(self):
        prev = read("SPY", suggestion="NO_EDGE_QUIET")
        curr = read("SPY", suggestion="ENTRIES_FAVORED_AT_SUPPORT")
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertTrue(any(a["kind"] == "suggestion" for a in alerts))

    def test_level_cross(self):
        prev = read("SPY", spot=744.0)   # below put wall 745
        curr = read("SPY", spot=746.0)   # above put wall 745
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertTrue(any(a["kind"] == "cross" and "put wall" in a["text"]
                            for a in alerts))

    def test_no_cross_same_side(self):
        prev = read("SPY", spot=744.0)
        curr = read("SPY", spot=743.0)
        alerts = diff_alerts("SPY", prev, curr, "2026-09-21")
        self.assertEqual(alerts, [])

    def test_first_run_no_spurious_alerts(self):
        # Empty prev (first ever evaluation): nothing to compare -> quiet.
        alerts = diff_alerts("SPY", {}, read("SPY"), "2026-09-21")
        self.assertEqual(alerts, [])


class TestMarketHoursGate(unittest.TestCase):
    def test_weekday_open(self):
        # Monday 2026-09-21, 10:00 ET -> open
        self.assertTrue(is_market_open(datetime(2026, 9, 21, 10, 0, tzinfo=ET)))

    def test_weekday_before_open(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 21, 8, 0, tzinfo=ET)))

    def test_weekday_after_close(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 21, 18, 0, tzinfo=ET)))

    def test_sunday_closed(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 20, 12, 0, tzinfo=ET)))

    def test_holiday_closed(self):
        # Labor Day 2026-09-07
        self.assertFalse(is_market_open(datetime(2026, 9, 7, 12, 0, tzinfo=ET)))

    def test_naive_assumed_et(self):
        self.assertTrue(is_market_open(datetime(2026, 9, 21, 10, 0)))


if __name__ == "__main__":
    unittest.main()
