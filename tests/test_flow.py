"""Unit tests for the turblance-trader flow engine (stdlib unittest, no network).

The package under test is in ../src (not pip-installed), so we add it to
sys.path here. This shim lives only in the test file.

Covers: cold-start baselines (never fabricated), NaN/zero handling, sweep
proxy scoring and ranking, multi-expiry sweep rollup, schema validation of
inputs, the baseline z-score math, and the exact FlowProfile contract a
later backtest step will duck-type against.
"""

import json
import math
import os
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

import pandas as pd

from turblance_trader.capture.schema import SchemaError
from turblance_trader.flow import (
    BaselineSet,
    FlowProfile,
    classify_trade_location,
    compute_baselines,
    compute_flow_profile,
    detect_sweeps,
    score_unusual_activity,
)


def _chain(rows, spot=100.0, quote_time="2026-09-18T15:30:00Z",
           expiry="2026-10-17", symbol="SPX"):
    """Build a chain DataFrame in the shared 16-column schema.

    Each row: (strike, option_type, bid, ask, last, volume, open_interest).
    None means NaN for bid/ask/last/volume; open_interest None means 0.
    Column ORDER matches the contract schema exactly (validate_schema
    requires exact order).
    """
    records = []
    for strike, option_type, bid, ask, last, volume, oi in rows:
        records.append(
            {
                "symbol": symbol,
                "quote_time": quote_time,
                "expiry": expiry,
                "strike": float(strike),
                "option_type": option_type,
                "bid": float("nan") if bid is None else float(bid),
                "ask": float("nan") if ask is None else float(ask),
                "last": float("nan") if last is None else float(last),
                "implied_volatility": 0.20,
                "open_interest": 0 if oi is None else int(oi),
                "volume": float("nan") if volume is None else int(volume),
                "delta": 0.5,
                "gamma": 0.05,
                "theta": -0.1,
                "vega": 0.2,
                "spot": float(spot),
            }
        )
    return pd.DataFrame(records)


def _lift_row(strike, option_type="call", volume=1000, oi=500,
              expiry="2026-10-17"):
    """One aggressively-lifted row: last prints at the offer."""
    return (strike, option_type, 1.00, 1.20, 1.18, volume, oi)


def _history(contract_rows, n_days=5, base_volume=100, base_oi=500):
    """Concatenated history snapshots for one set of contracts.

    ``contract_rows``: list of (strike, option_type); each gets ``n_days``
    of history with volumes base_volume +/- small jitter and OI base_oi.
    Returns a DataFrame in schema column order.
    """
    frames = []
    for day in range(n_days):
        qt = f"2026-09-{10 + day:02d}T15:30:00Z"
        rows = [
            (s, t, 1.00, 1.20, 1.10, base_volume + ((day * 7 + i) % 5) - 2,
             base_oi + day)
            for i, (s, t) in enumerate(contract_rows)
        ]
        frames.append(_chain(rows, quote_time=qt))
    return pd.concat(frames, ignore_index=True)


class TestTradeLocation(unittest.TestCase):
    def test_lifted_hit_mid(self):
        self.assertEqual(classify_trade_location(1.0, 1.2, 1.18), "lifted")
        self.assertEqual(classify_trade_location(1.0, 1.2, 1.02), "hit")
        self.assertEqual(classify_trade_location(1.0, 1.2, 1.10), "mid")

    def test_unusable_spread_returns_none(self):
        self.assertIsNone(classify_trade_location(1.0, 1.0, 1.0))   # zero width
        self.assertIsNone(classify_trade_location(1.2, 1.0, 1.1))   # crossed
        self.assertIsNone(classify_trade_location(None, 1.2, 1.1))  # NaN bid
        self.assertIsNone(classify_trade_location(1.0, 1.2, None))  # NaN last


class TestBaselines(unittest.TestCase):
    def test_zscore_math(self):
        # Volumes [10, 12, 11, 13, 12]: median 12, MAD 1.
        # z(18) = 6 / (1.4826 * 1) ~= 4.047.
        hist = _history([(100.0, "call")], n_days=5, base_volume=12,
                        base_oi=100)
        # Force exact volumes: rewrite the volume column deterministically.
        hist["volume"] = [10, 12, 11, 13, 12]
        hist["open_interest"] = [100, 100, 100, 100, 100]
        bl = compute_baselines(hist, min_samples=5)
        now = _chain([(100.0, "call", 1.0, 1.2, 1.1, 18, 100)])
        scored = score_unusual_activity(now, baselines=bl)
        vz = scored.loc[0, "volume_z"]
        self.assertAlmostEqual(vz, 6.0 / 1.4826, places=3)
        # Flat OI history (MAD 0) with unchanged OI -> z 0, not NaN/inf.
        self.assertEqual(scored.loc[0, "oi_z"], 0.0)
        self.assertTrue(scored.loc[0, "has_baseline"])
        self.assertEqual(scored.loc[0, "baseline_source"], "contract")

    def test_bucket_fallback_when_contract_history_thin(self):
        # Only 2 samples for the contract (< min_samples 5) but the
        # bucket (same expiry/type, several strikes) has enough pooled.
        contracts = [(95.0, "call"), (100.0, "call"), (105.0, "call")]
        hist = _history(contracts, n_days=2, base_volume=50, base_oi=200)
        bl = compute_baselines(hist, min_samples=5)
        self.assertEqual(bl.n_contracts, 0)  # too thin per contract
        self.assertGreater(bl.n_buckets, 0)  # bucket pooled across strikes
        now = _chain([(100.0, "call", 1.0, 1.2, 1.1, 500, 200)])
        scored = score_unusual_activity(now, baselines=bl)
        self.assertTrue(scored.loc[0, "has_baseline"])
        self.assertEqual(scored.loc[0, "baseline_source"], "bucket")
        self.assertFalse(math.isnan(scored.loc[0, "volume_z"]))

    def test_insufficient_everywhere_means_no_baseline(self):
        hist = _history([(100.0, "call")], n_days=1, base_volume=10,
                        base_oi=10)
        bl = compute_baselines(hist, min_samples=5)
        now = _chain([(100.0, "call", 1.0, 1.2, 1.1, 9999, 10)])
        scored = score_unusual_activity(now, baselines=bl)
        self.assertFalse(scored.loc[0, "has_baseline"])
        self.assertIsNone(scored.loc[0, "baseline_source"])
        self.assertTrue(math.isnan(scored.loc[0, "volume_z"]))
        self.assertTrue(math.isnan(scored.loc[0, "contract_score"]))

    def test_cold_start_never_fabricates(self):
        now = _chain([_lift_row(100.0, volume=10_000)])
        scored = score_unusual_activity(now, baselines=None)  # no history
        self.assertTrue(scored["volume_z"].isna().all())
        self.assertTrue(scored["contract_score"].isna().all())
        self.assertFalse(scored["has_baseline"].any())
        prof = compute_flow_profile(now, baselines=None)
        self.assertEqual(prof.unusual_strikes, [])
        self.assertTrue(prof.cold_start)

    def test_empty_history_yields_empty_baselineset(self):
        bl = compute_baselines(pd.DataFrame())
        self.assertIsInstance(bl, BaselineSet)
        self.assertEqual(bl.n_contracts, 0)
        key = ("SPX", "2026-10-17", 100.0, "call")
        self.assertEqual(bl.lookup(key, ("SPX", "2026-10-17", "call")),
                         (None, None, None, None, None))

    def test_oi_change_needs_prev_snapshot(self):
        now = _chain([(100.0, "call", 1.0, 1.2, 1.1, 50, 120)])
        hist = _history([(100.0, "call")], n_days=5, base_oi=100)
        bl = compute_baselines(hist, min_samples=5)
        scored = score_unusual_activity(now, baselines=bl)  # no prev_df
        self.assertTrue(math.isnan(scored.loc[0, "oi_change"]))
        prev = _chain([(100.0, "call", 1.0, 1.2, 1.1, 40, 100)])
        scored2 = score_unusual_activity(now, prev_df=prev, baselines=bl)
        self.assertEqual(scored2.loc[0, "oi_change"], 20.0)


class TestSweepProxy(unittest.TestCase):
    def _sweep_chain(self, expiry="2026-10-17", n_strikes=5):
        hist_rows = [(95.0 + 5 * i, "call") for i in range(n_strikes)]
        hist = _history(hist_rows, n_days=5, base_volume=40, base_oi=300)
        rows = [_lift_row(95.0 + 5 * i, volume=2000) for i in range(n_strikes)]
        return _chain(rows, expiry=expiry), compute_baselines(hist,
                                                             min_samples=5)

    def test_detects_multi_strike_call_lift(self):
        df, bl = self._sweep_chain()
        sweeps = detect_sweeps(df, baselines=bl)
        self.assertEqual(len(sweeps), 1)
        sw = sweeps[0]
        self.assertEqual(sw["direction"], "calls")
        self.assertEqual(sw["side"], "lifted")
        self.assertEqual(sw["strikes_hit"], [95.0, 100.0, 105.0, 110.0,
                                             115.0])
        self.assertEqual(sw["expiry"], "2026-10-17")
        self.assertGreater(sw["score"], 0.0)
        self.assertTrue(sw["proxy"])
        self.assertTrue(sw["experimental"])

    def test_control_mixed_directions_not_a_sweep(self):
        # Same elevated volume but alternating lift/hit and call/put:
        # no single-direction cluster reaches min_strikes.
        hist = _history([(95.0, "call"), (100.0, "put")], n_days=5,
                        base_volume=40, base_oi=300)
        bl = compute_baselines(hist, min_samples=5)
        rows = [
            (95.0, "call", 1.00, 1.20, 1.18, 2000, 300),   # lifted call
            (100.0, "put", 1.00, 1.20, 1.02, 2000, 300),   # hit put
            (95.0, "put", 1.00, 1.20, 1.10, 2000, 300),    # mid put
        ]
        df = _chain(rows)
        sweeps = detect_sweeps(df, baselines=bl)
        self.assertEqual(sweeps, [])

    def test_below_min_strikes_not_a_sweep(self):
        df, bl = self._sweep_chain(n_strikes=2)
        sweeps = detect_sweeps(df, baselines=bl, min_strikes=3)
        self.assertEqual(sweeps, [])

    def test_ranking_by_score(self):
        df1, bl = self._sweep_chain(n_strikes=5)   # strong: volume 2000
        # Weaker but still burst-like: volume 46 -> z ~= 4.05 (above the
        # 2.0 leg cutoff, below the 6.0 z-cap), so its score must rank
        # below the saturated 2000-volume sweep.
        rows2 = [_lift_row(95.0 + 5 * i, volume=46) for i in range(5)]
        df2 = _chain(rows2)
        s1 = detect_sweeps(df1, baselines=bl)
        s2 = detect_sweeps(df2, baselines=bl)
        self.assertEqual(len(s1), 1)
        self.assertEqual(len(s2), 1)
        self.assertGreater(s1[0]["score"], s2[0]["score"])

    def test_cold_start_burst_fallback_flagged(self):
        # No baselines: absolute-volume fallback fires and says so.
        rows = [_lift_row(95.0 + 5 * i, volume=5000) for i in range(4)]
        df = _chain(rows)
        sweeps = detect_sweeps(df, baselines=None, burst_volume_min=1000)
        self.assertEqual(len(sweeps), 1)
        self.assertTrue(sweeps[0]["used_burst_fallback"])
        # Tiny volumes -> nothing, even cold.
        rows_small = [_lift_row(95.0 + 5 * i, volume=10) for i in range(4)]
        df_small = _chain(rows_small)
        self.assertEqual(
            detect_sweeps(df_small, baselines=None, burst_volume_min=1000),
            [])

    def test_multi_expiry_rollup_is_one_event(self):
        # Same sweep footprint on a weekly and a monthly: ONE event.
        df_w, bl_w = self._sweep_chain(expiry="2026-09-25", n_strikes=4)
        df_m, bl_m = self._sweep_chain(expiry="2026-10-17", n_strikes=4)
        df = pd.concat([df_w, df_m], ignore_index=True)
        # History covering both expiries.
        frames = []
        for exp in ("2026-09-25", "2026-10-17"):
            h = _history([(95.0 + 5 * i, "call") for i in range(4)],
                         n_days=5, base_volume=40, base_oi=300)
            h["expiry"] = exp
            frames.append(h)
        bl = compute_baselines(pd.concat(frames, ignore_index=True),
                               min_samples=5)
        sweeps = detect_sweeps(df, baselines=bl)
        self.assertEqual(len(sweeps), 1)
        sw = sweeps[0]
        self.assertEqual(sorted(sw["expiries"]),
                         ["2026-09-25", "2026-10-17"])
        self.assertEqual(sw["direction"], "calls")
        # Contract keys preserved on the merged event.
        self.assertIn("expiry", sw)
        self.assertIn("strikes_hit", sw)
        self.assertIn("volume", sw)
        self.assertIn("score", sw)
        self.assertEqual(set(sw["strikes_hit_by_expiry"]),
                         {"2026-09-25", "2026-10-17"})


class TestNanZeroHandling(unittest.TestCase):
    def test_nan_volume_zero_oi_crossed_spread(self):
        rows = [
            (95.0, "call", 1.0, 1.2, 1.1, None, 0),    # NaN volume, 0 OI
            (100.0, "call", 1.0, 1.0, 1.0, 50, 0),     # crossed/zero spread
            (105.0, "put", None, None, None, 60, 10),  # all-NaN quote
            (110.0, "put", 1.0, 1.2, 1.1, 0, 5),       # zero volume
        ]
        df = _chain(rows)
        hist = _history([(95.0, "call"), (100.0, "call"), (105.0, "put"),
                         (110.0, "put")], n_days=5, base_volume=30,
                        base_oi=20)
        bl = compute_baselines(hist, min_samples=5)
        prof = compute_flow_profile(df, baselines=bl)  # must not raise
        self.assertIsInstance(prof, FlowProfile)
        self.assertTrue(math.isnan(
            prof.by_strike.loc[prof.by_strike["strike"] == 95.0,
                               "volume_z"].iloc[0]))
        # Totals treat NaN volume as 0.
        self.assertEqual(
            prof.by_strike.loc[prof.by_strike["strike"] == 95.0,
                               "call_volume"].iloc[0], 0.0)
        # Nothing to sweep on: no crash, no phantom sweeps.
        self.assertEqual(prof.sweeps, [])
        d = prof.to_dict()
        json.dumps(d)  # JSON-serializable even with NaNs inside

    def test_all_nan_volume_cold(self):
        df = _chain([(100.0, "call", 1.0, 1.2, 1.1, None, 0)])
        prof = compute_flow_profile(df)  # cold start, NaN volume
        self.assertEqual(prof.by_strike["call_volume"].iloc[0], 0.0)
        self.assertEqual(prof.unusual_strikes, [])


class TestSchemaValidation(unittest.TestCase):
    def test_wrong_columns_rejected(self):
        df = _chain([(100.0, "call", 1.0, 1.2, 1.1, 10, 5)]).drop(
            columns=["vega"])
        with self.assertRaises(SchemaError):
            compute_flow_profile(df)

    def test_bad_option_type_rejected(self):
        df = _chain([(100.0, "CALL", 1.0, 1.2, 1.1, 10, 5)])
        with self.assertRaises(SchemaError):
            compute_flow_profile(df)

    def test_prev_df_also_validated(self):
        df = _chain([(100.0, "call", 1.0, 1.2, 1.1, 10, 5)])
        prev = df.drop(columns=["theta"])
        with self.assertRaises(SchemaError):
            compute_flow_profile(df, prev_df=prev)

    def test_history_schema_validated(self):
        hist = _history([(100.0, "call")], n_days=5).drop(columns=["spot"])
        with self.assertRaises(SchemaError):
            compute_baselines(hist)


class TestProfileContract(unittest.TestCase):
    """The exact attribute names a later backtest step duck-types against."""

    def _profile(self):
        hist = _history([(95.0, "call"), (100.0, "call"), (105.0, "call"),
                         (100.0, "put")], n_days=5, base_volume=40,
                        base_oi=300)
        bl = compute_baselines(hist, min_samples=5)
        rows = [_lift_row(95.0, volume=3000),
                _lift_row(100.0, volume=3000),
                _lift_row(105.0, volume=3000),
                (100.0, "put", 1.0, 1.2, 1.1, 45, 300)]
        df = _chain(rows)
        return compute_flow_profile(df, baselines=bl), bl

    def test_attribute_names_exact(self):
        prof, _ = self._profile()
        for attr in ("by_strike", "by_expiry", "spot", "unusual_strikes",
                     "sweeps", "to_dict"):
            self.assertTrue(hasattr(prof, attr), f"missing .{attr}")
        self.assertIsInstance(prof.spot, float)

    def test_by_strike_columns_exact(self):
        prof, _ = self._profile()
        self.assertEqual(
            list(prof.by_strike.columns),
            ["strike", "call_volume", "put_volume", "volume_z",
             "oi_change", "sweep_score", "unusual_score"],
        )

    def test_by_expiry_columns(self):
        prof, _ = self._profile()
        for col in ("expiry", "total_volume", "unusual_score"):
            self.assertIn(col, prof.by_expiry.columns)

    def test_unusual_strikes_shape(self):
        prof, _ = self._profile()
        self.assertGreater(len(prof.unusual_strikes), 0)
        for u in prof.unusual_strikes:
            for key in ("strike", "option_type", "score", "reason"):
                self.assertIn(key, u)
            self.assertIn(u["option_type"], ("call", "put"))
            self.assertIsInstance(u["score"], float)
            self.assertIsInstance(u["reason"], str)

    def test_sweeps_shape(self):
        prof, _ = self._profile()
        self.assertEqual(len(prof.sweeps), 1)
        sw = prof.sweeps[0]
        for key in ("expiry", "direction", "strikes_hit", "volume",
                    "score"):
            self.assertIn(key, sw)
        self.assertIsInstance(sw["strikes_hit"], list)

    def test_by_expiry_rollup(self):
        prof, _ = self._profile()
        be = prof.by_expiry
        self.assertEqual(len(be), 1)
        self.assertEqual(be["total_volume"].iloc[0], 9045.0)
        # Loud strikes lift the expiry's unusual_score (max of strikes).
        self.assertGreater(be["unusual_score"].iloc[0], 2.0)

    def test_to_dict_json_serializable(self):
        prof, _ = self._profile()
        d = prof.to_dict()
        text = json.dumps(d)
        back = json.loads(text)
        self.assertEqual(back["n_unusual_strikes"],
                         len(prof.unusual_strikes))
        self.assertEqual(back["n_sweeps"], len(prof.sweeps))
        self.assertIn("by_strike", back)
        self.assertIn("by_expiry", back)

    def test_spot_is_median(self):
        prof, _ = self._profile()
        self.assertEqual(prof.spot, 100.0)


if __name__ == "__main__":
    unittest.main()
