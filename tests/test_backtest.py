"""Unit tests for the backtest harness (loader, signals, evaluate).

*** SYNTHETIC DATA ONLY ***
Every fixture in this file is hand-built fake data. These tests verify that
the harness *machinery* works (loading, signal shapes, scoring logic) — they
prove NOTHING about whether any GEX signal works on real markets. Real
validation requires forward-captured data; see backtests/reports/*.md.

Stdlib unittest only. No network calls.
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import pandas as pd  # noqa: E402

from backtests.harness import evaluate, loader, signals  # noqa: E402
from backtests.harness.loader import DataStoreError  # noqa: E402

UTC = timezone.utc

CAPTURE_COLUMNS = [
    "symbol", "quote_time", "expiry", "strike", "option_type", "bid", "ask",
    "last", "implied_volatility", "open_interest", "volume", "delta",
    "gamma", "theta", "vega", "spot",
]


def make_chain_rows(symbol, quote_time, spot, strikes=(495.0, 500.0, 505.0)):
    rows = []
    for strike in strikes:
        for otype in ("call", "put"):
            rows.append({
                "symbol": symbol,
                "quote_time": quote_time.isoformat(),
                "expiry": "2026-10-16",
                "strike": strike,
                "option_type": otype,
                "bid": 1.0, "ask": 1.2, "last": 1.1,
                "implied_volatility": 0.20,
                "open_interest": 1000, "volume": 50,
                "delta": 0.5, "gamma": 0.05, "theta": -0.01, "vega": 0.10,
                "spot": spot,
            })
    return rows


class FakeProfile:
    """Duck-typed stand-in for the GEX-engine profile contract (synthetic)."""

    def __init__(self, spot=500.0, king_strike=500.0, call_wall=505.0,
                 put_wall=495.0, regime="positive"):
        self.spot = spot
        self.king_node = {"strike": king_strike, "net_gex": -1e9}
        self.call_wall = call_wall
        self.put_wall = put_wall
        self.zero_gamma_flip = 499.0
        self.regime = regime
        self.by_strike = pd.DataFrame([
            {"strike": 495.0, "call_gex": 1.0, "put_gex": 2.0, "net_gex": 3.0},
            {"strike": king_strike, "call_gex": -5.0, "put_gex": -6.0,
             "net_gex": -11.0},
            {"strike": 505.0, "call_gex": 4.0, "put_gex": 1.0, "net_gex": 5.0},
        ])
        self.by_expiry = pd.DataFrame()

    def to_dict(self):
        return {"spot": self.spot, "regime": self.regime}


class LoaderTest(unittest.TestCase):
    """loader reads fixture snapshots (synthetic) in time order."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "chains")
        day1 = os.path.join(self.store, "SPY", "2026-09-18")
        day2 = os.path.join(self.store, "SPY", "2026-09-19")
        os.makedirs(day1)
        os.makedirs(day2)
        self._write(day1, "snapshot_143000_utc.csv",
                    datetime(2026, 9, 18, 14, 30, tzinfo=UTC), 500.0)
        self._write(day1, "snapshot_150000_utc.csv",
                    datetime(2026, 9, 18, 15, 0, tzinfo=UTC), 501.0)
        self._write(day2, "snapshot_143000_utc.csv",
                    datetime(2026, 9, 19, 14, 30, tzinfo=UTC), 502.0)

    def _write(self, day_dir, fname, quote_time, spot):
        with open(os.path.join(day_dir, fname), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CAPTURE_COLUMNS)
            w.writeheader()
            w.writerows(make_chain_rows("SPY", quote_time, spot))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_loads_snapshots_in_time_order(self):
        snaps = loader.load_snapshots("SPY", store_root=self.store)
        self.assertEqual(len(snaps), 3)
        times = [t for t, _ in snaps]
        self.assertEqual(times, sorted(times))
        self.assertEqual(
            [t.isoformat() for t in times],
            ["2026-09-18T14:30:00+00:00",
             "2026-09-18T15:00:00+00:00",
             "2026-09-19T14:30:00+00:00"],
        )
        for _, df in snaps:
            self.assertTrue(
                set(loader.REQUIRED_COLUMNS).issubset(df.columns))

    def test_csv_fallback_used_when_no_parquet(self):
        snaps = loader.load_snapshots("SPY", store_root=self.store)
        self.assertEqual(len(snaps), 3)  # only .csv files exist

    def test_missing_symbol_raises_clear_error(self):
        with self.assertRaises(DataStoreError) as cm:
            loader.load_snapshots("QQQ", store_root=self.store)
        self.assertIn("QQQ", str(cm.exception))

    def test_missing_store_raises_clear_error(self):
        with self.assertRaises(DataStoreError):
            loader.load_snapshots("SPY", store_root=os.path.join(self.tmp, "nope"))

    def test_malformed_snapshot_raises_clear_error(self):
        bad_day = os.path.join(self.store, "SPY", "2026-09-20")
        os.makedirs(bad_day)
        with open(os.path.join(bad_day, "snapshot_143000_utc.csv"), "w") as f:
            f.write("symbol,strike\nSPY,500\n")
        with self.assertRaises(DataStoreError) as cm:
            loader.load_snapshots("SPY", store_root=self.store)
        self.assertIn("missing required columns", str(cm.exception))

    def test_list_symbols_and_days(self):
        self.assertEqual(loader.list_symbols(self.store), ["SPY"])
        self.assertEqual(loader.list_days("SPY", self.store),
                         ["2026-09-18", "2026-09-19"])


class SignalShapeTest(unittest.TestCase):
    """Each signal returns the documented shape and is labeled experimental."""

    def test_king_magnet_shape(self):
        out = signals.king_magnet(FakeProfile(spot=498.0, king_strike=500.0))
        self.assertEqual(out["signal"], "king_magnet")
        self.assertTrue(out["experimental"])
        self.assertEqual(out["levels"], [500.0])
        self.assertEqual(out["bias"], "bullish")  # king above spot
        self.assertIn("rationale", out)
        self.assertIn("details", out)

    def test_king_magnet_bearish_and_neutral(self):
        self.assertEqual(
            signals.king_magnet(FakeProfile(spot=502.0))["bias"], "bearish")
        self.assertEqual(
            signals.king_magnet(FakeProfile(spot=500.0))["bias"], "neutral")

    def test_king_magnet_no_king(self):
        p = FakeProfile()
        p.king_node = None
        p.by_strike = pd.DataFrame(
            columns=["strike", "call_gex", "put_gex", "net_gex"])
        out = signals.king_magnet(p)
        self.assertEqual(out["bias"], "neutral")
        self.assertEqual(out["levels"], [])

    def test_node_tap_decay_first_touch(self):
        ctx = signals.new_context()
        out = signals.node_tap_decay(FakeProfile(), spot=499.9, context=ctx)
        self.assertTrue(out["experimental"])
        self.assertEqual(out["details"]["node"], "king")
        self.assertEqual(out["details"]["touch_count"], 1)
        self.assertAlmostEqual(
            out["details"]["predicted_reaction_prob"], 0.80)
        self.assertEqual(out["bias"], "fade")

    def test_node_tap_decay_count_increments_per_touch(self):
        ctx = signals.new_context()
        p = FakeProfile()
        signals.node_tap_decay(p, spot=499.9, context=ctx)   # touch 1 (king)
        signals.node_tap_decay(p, spot=498.0, context=ctx)  # away: resets
        out = signals.node_tap_decay(p, spot=500.1, context=ctx)  # touch 2
        self.assertEqual(out["details"]["touch_count"], 2)
        self.assertAlmostEqual(
            out["details"]["predicted_reaction_prob"], 0.66)

    def test_node_tap_decay_no_touch_is_neutral(self):
        out = signals.node_tap_decay(FakeProfile(), spot=490.0,
                                     context=signals.new_context())
        self.assertEqual(out["bias"], "neutral")
        self.assertNotIn("touch_count", out["details"])

    def test_gamma_regime_mapping(self):
        self.assertEqual(
            signals.gamma_regime(FakeProfile(regime="positive"))["bias"], "range")
        self.assertEqual(
            signals.gamma_regime(FakeProfile(regime="negative"))["bias"], "trend")
        self.assertEqual(
            signals.gamma_regime(FakeProfile(regime="mixed"))["bias"], "whipsaw")
        out = signals.gamma_regime(FakeProfile(regime="positive"))
        self.assertTrue(out["experimental"])
        self.assertEqual(out["levels"], [495.0, 505.0])  # [put_wall, call_wall]

    def test_run_all_returns_three_signals(self):
        outs = signals.run_all(FakeProfile(), spot=500.0,
                               context=signals.new_context())
        self.assertEqual(len(outs), 3)
        self.assertEqual(
            [o["signal"] for o in outs],
            ["king_magnet", "node_tap_decay", "gamma_regime"])
        self.assertTrue(all(o["experimental"] for o in outs))


def _session(date, profile, price_list, regime="positive"):
    t0 = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
    prices = [(t0 + timedelta(minutes=5 * i), p)
              for i, p in enumerate(price_list)]
    return {
        "date": date,
        "profiles": [(t0, profile)],
        "prices": prices,
        "regime": regime,
    }


class EvaluateTest(unittest.TestCase):
    """evaluate.py scores a hand-built synthetic scenario correctly."""

    def test_king_magnet_hit_recorded(self):
        king = FakeProfile(king_strike=500.0)
        hit = _session("2026-09-18", king, [498.0, 499.9, 501.0])   # touches
        miss = _session("2026-09-19", king, [495.0, 496.0, 497.0])  # never near
        res = evaluate.king_magnet_hit_rate([hit, miss])
        self.assertEqual(res["n_scored"], 2)
        self.assertEqual(res["hits"], 1)
        self.assertAlmostEqual(res["hit_rate"], 0.5)
        self.assertEqual(
            [d["hit"] for d in res["per_session"]], [True, False])

    def test_tap_decay_bucketing_and_reactions(self):
        # Synthetic: touch1 -> reaction, touch2 -> breakthrough, touch3 -> reaction.
        king = FakeProfile(king_strike=500.0)
        prices = [
            498.0,   # away
            499.9,   # touch #1 (from below)
            499.1,   # bounce down -> reaction
            497.5,   # away
            500.2,   # touch #2 (from above)
            499.3,   # falls through -> breakthrough, no reaction
            498.0,   # away
            500.15,  # touch #3 (from above)
            500.9,   # bounce up -> reaction
        ]
        res = evaluate.tap_decay_observed(
            [_session("2026-09-18", king, prices)])
        b = res["buckets"]
        self.assertEqual((b[1]["touches"], b[1]["reactions"]), (1, 1))
        self.assertEqual((b[2]["touches"], b[2]["reactions"]), (1, 0))
        self.assertEqual((b[3]["touches"], b[3]["reactions"]), (1, 1))
        self.assertEqual(b["4+"]["touches"], 0)
        self.assertIsNone(b["4+"]["observed_rate"])
        self.assertAlmostEqual(b[1]["observed_rate"], 1.0)
        self.assertAlmostEqual(b[2]["observed_rate"], 0.0)
        # Predicted schedule is the claim under test, reported alongside.
        self.assertEqual(
            res["predicted_schedule"], {"1": 0.80, "2": 0.66, "3": 0.33, "4+": 0.10})

    def test_regime_range_accuracy(self):
        prof = FakeProfile()
        sessions = [
            _session("2026-09-18", prof, [100, 100.5, 99.8], "positive"),
            _session("2026-09-19", prof, [100, 100.3, 99.9], "positive"),
            _session("2026-09-20", prof, [100, 103.0, 98.0], "negative"),
            _session("2026-09-21", prof, [100, 104.0, 97.0], "negative"),
        ]
        res = evaluate.regime_range_accuracy(sessions)
        self.assertEqual(res["n_scored"], 4)
        self.assertAlmostEqual(res["accuracy"], 1.0)
        self.assertTrue(res["direction_match"])
        self.assertLess(res["mean_range_pct_positive"],
                        res["mean_range_pct_negative"])

    def test_costs_hook_defaults_documented(self):
        self.assertIn("slippage_bps", evaluate.DEFAULT_COSTS)
        res = evaluate.king_magnet_hit_rate(
            [_session("2026-09-18", FakeProfile(), [499.9, 500.1])])
        self.assertIn("note", res["costs"])

    def test_empty_sessions_raise(self):
        with self.assertRaises(evaluate.InsufficientDataError):
            evaluate.king_magnet_hit_rate([])
        with self.assertRaises(evaluate.InsufficientDataError):
            evaluate.tap_decay_observed([])
        with self.assertRaises(evaluate.InsufficientDataError):
            evaluate.regime_range_accuracy([])

    def test_run_all_combines_metrics(self):
        king = FakeProfile(king_strike=500.0)
        sessions = [
            _session("2026-09-18", king, [498.0, 499.9, 501.0], "positive"),
            _session("2026-09-19", king, [495.0, 496.0, 497.0], "negative"),
        ]
        res = evaluate.run_all(sessions)
        self.assertIn("king_magnet", res)
        self.assertIn("tap_decay", res)
        self.assertIn("regime", res)
        self.assertEqual(res["n_sessions"], 2)


class RunValidationCLITest(unittest.TestCase):
    """The CLI exits with 'insufficient data' instead of fabricating results."""

    def test_insufficient_data_exit_code(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "backtests", "harness"))
        import run_validation
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(SystemExit) as cm:
                run_validation.main(
                    ["--symbol", "SPY", "--data-root", os.path.join(tmp, "empty")])
            self.assertEqual(cm.exception.code, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
