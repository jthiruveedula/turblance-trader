"""Unit tests for the turblance-trader GEX engine (stdlib unittest, no network).

The package under test is in ../src (not pip-installed), so we add it to
sys.path here. This shim lives only in the test file.
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

from turblance_trader.gex import compute_profile, gex_velocity
from turblance_trader.gex import black_scholes
from turblance_trader.gex import exposure


def _chain(rows, spot=100.0, quote_time="2026-09-18T15:30:00Z", expiry="2026-10-17"):
    """Build a chain DataFrame from compact row specs.

    Each row: (strike, option_type, gamma, open_interest, implied_volatility).
    gamma/implied_volatility may be None to mean NaN.
    """
    records = []
    for strike, option_type, g, oi, iv in rows:
        records.append(
            {
                "symbol": "SPX",
                "quote_time": quote_time,
                "expiry": expiry,
                "strike": float(strike),
                "option_type": option_type,
                "bid": 1.0,
                "ask": 1.1,
                "last": 1.05,
                "implied_volatility": float("nan") if iv is None else float(iv),
                "open_interest": int(oi),
                "volume": 10,
                "delta": 0.5,
                "gamma": float("nan") if g is None else float(g),
                "theta": -0.1,
                "vega": 0.2,
                "spot": float(spot),
            }
        )
    return pd.DataFrame(records)


class TestBlackScholes(unittest.TestCase):
    def test_gamma_atm_forward_identity(self):
        # Concrete numeric case: S=K=100, sigma=0.2, T=1, r=-sigma^2/2 gives
        # d1 = 0 exactly, so gamma must equal 1 / (S * sigma * sqrt(2*pi*T)).
        s, k, sigma, t, r = 100.0, 100.0, 0.2, 1.0, -0.02
        got = black_scholes.gamma(s, k, t, sigma, risk_free_rate=r)
        expected = 1.0 / (s * sigma * math.sqrt(2.0 * math.pi * t))
        self.assertAlmostEqual(got, expected, places=12)
        self.assertAlmostEqual(got, 0.0199471140200716, places=12)

    def test_gamma_is_positive_and_finite(self):
        g = black_scholes.gamma(100.0, 95.0, 0.5, 0.25)
        self.assertTrue(math.isfinite(g) and g > 0.0)

    def test_gamma_degenerate_inputs_give_finite_zero(self):
        # T <= 0, sigma <= 0, bad prices -> gamma 0, never NaN, never raises.
        for kwargs in (
            dict(time_to_expiry=0.0, volatility=0.2),
            dict(time_to_expiry=-1.0, volatility=0.2),
            dict(time_to_expiry=1.0, volatility=0.0),
            dict(time_to_expiry=1.0, volatility=-0.5),
            dict(time_to_expiry=float("nan"), volatility=0.2),
        ):
            g = black_scholes.gamma(100.0, 100.0, kwargs["time_to_expiry"], kwargs["volatility"])
            self.assertEqual(g, 0.0, f"expected 0 for {kwargs}")
            self.assertTrue(math.isfinite(g))
        self.assertEqual(black_scholes.gamma(0.0, 100.0, 1.0, 0.2), 0.0)
        self.assertEqual(black_scholes.gamma(100.0, -5.0, 1.0, 0.2), 0.0)

    def test_delta_put_call_parity(self):
        call = black_scholes.delta(100.0, 100.0, 1.0, 0.2, "call")
        put = black_scholes.delta(100.0, 100.0, 1.0, 0.2, "put")
        self.assertAlmostEqual(call - put, 1.0, places=12)

    def test_delta_degenerate_collapses_to_intrinsic(self):
        self.assertEqual(black_scholes.delta(110.0, 100.0, 0.0, 0.2, "call"), 1.0)
        self.assertEqual(black_scholes.delta(90.0, 100.0, 0.0, 0.2, "call"), 0.0)
        self.assertEqual(black_scholes.delta(90.0, 100.0, 0.0, 0.2, "put"), -1.0)
        self.assertEqual(black_scholes.delta(110.0, 100.0, 1.0, 0.0, "put"), 0.0)


class TestExposure(unittest.TestCase):
    def test_single_contract_gex_matches_hand_computation(self):
        # gamma=0.05, OI=100, spot=100:
        #   0.05 * 100 * 100 * 100^2 * 0.01 = 50_000 (customer, long options)
        df = _chain([(100.0, "call", 0.05, 100, None)])
        for position, expected in (("short", -50000.0), ("long", 50000.0), ("flat", 0.0)):
            profile = compute_profile(df, spot=100.0, dealer_position=position)
            row = profile.by_strike.iloc[0]
            self.assertAlmostEqual(row["call_gex"], expected, places=6)
            self.assertAlmostEqual(row["put_gex"], 0.0, places=9)
            self.assertAlmostEqual(row["net_gex"], expected, places=6)

    def test_dealer_position_must_be_valid(self):
        df = _chain([(100.0, "call", 0.05, 100, None)])
        with self.assertRaises(ValueError):
            compute_profile(df, dealer_position="sideways")

    def test_missing_gamma_falls_back_to_black_scholes(self):
        # gamma NaN, IV 0.2 -> BS gamma; quote 2026-09-18, expiry 2026-10-17
        # gives T = 29/365 years.
        df = _chain([(100.0, "call", None, 100, 0.2)])
        out, meta = exposure.compute_exposure(df, risk_free_rate=0.0)
        self.assertEqual(meta["skipped_rows"], 0)
        t = 29.0 / 365.0
        expected_gamma = black_scholes.gamma(100.0, 100.0, t, 0.2, risk_free_rate=0.0)
        self.assertGreater(expected_gamma, 0.0)
        self.assertAlmostEqual(out["resolved_gamma"].iloc[0], expected_gamma, places=12)
        expected_gex = -1.0 * expected_gamma * 100 * 100 * 100.0**2 * 0.01
        self.assertAlmostEqual(out["dealer_gex"].iloc[0], expected_gex, places=6)

    def test_rows_missing_both_gamma_and_iv_are_skipped_and_counted(self):
        df = _chain(
            [
                (100.0, "call", 0.05, 100, None),  # usable
                (105.0, "put", None, 100, None),   # skipped
                (110.0, "call", None, 100, None),  # skipped
            ]
        )
        profile = compute_profile(df, spot=100.0)
        self.assertEqual(profile.skipped_rows, 2)
        self.assertEqual(profile.n_rows, 3)
        self.assertEqual(list(profile.by_strike["strike"]), [100.0])
        self.assertIn("skipped_rows", profile.to_dict())

    def test_zero_time_and_zero_vol_fallbacks_are_finite_zeros(self):
        # Same-day expiry -> T = 0; IV = 0 -> sigma = 0. Both must resolve to
        # gamma 0 with no NaN leaking into exposure.
        df = _chain(
            [
                (100.0, "call", None, 100, 0.2),  # T = 0 path
                (105.0, "call", None, 100, 0.0),  # sigma = 0 path
            ],
            quote_time="2026-10-17T15:30:00Z",
            expiry="2026-10-17",
        )
        out, meta = exposure.compute_exposure(df)
        self.assertEqual(meta["skipped_rows"], 0)
        self.assertTrue((out["resolved_gamma"] == 0.0).all())
        self.assertTrue((out["dealer_gex"] == 0.0).all())
        self.assertTrue(out["resolved_gamma"].notna().all())
        self.assertTrue(out["dealer_gex"].notna().all())


class TestLevels(unittest.TestCase):
    def test_zero_gamma_flip_interpolation(self):
        # dealer_position='long' keeps our synthetic signs intact.
        # spot=95, OI=100 -> per-unit gex divisor 100*100*95^2*0.01 = 902500.
        df = _chain(
            [
                (90.0, "call", 1000.0 / 902500.0, 100, None),
                (100.0, "call", -1000.0 / 902500.0, 100, None),
            ],
            spot=95.0,
        )
        profile = compute_profile(df, spot=95.0, dealer_position="long")
        # nets are +1000 @90 and -1000 @100 -> linear crossing at 95.
        self.assertAlmostEqual(profile.zero_gamma_flip, 95.0, places=9)

    def test_zero_gamma_flip_none_without_crossing(self):
        df = _chain(
            [
                (90.0, "call", 0.001, 100, None),
                (100.0, "call", 0.002, 100, None),
            ]
        )
        profile = compute_profile(df, dealer_position="long")
        self.assertIsNone(profile.zero_gamma_flip)

    def test_zero_gamma_flip_ignores_zero_exposure_tails(self):
        # Regression: far-tail strikes with no open interest carry zero GEX.
        # A leading run of zero-exposure strikes must not anchor the flip;
        # uniformly negative exposure elsewhere means no genuine crossing.
        df = _chain(
            [
                (50.0, "call", 0.001, 0, None),
                (55.0, "call", 0.001, 0, None),
                (90.0, "call", -0.001, 100, None),
                (100.0, "call", -0.002, 100, None),
            ],
            spot=95.0,
        )
        profile = compute_profile(df, spot=95.0, dealer_position="long")
        self.assertIsNone(profile.zero_gamma_flip)

    def test_walls_and_king_node(self):
        # spot=100, dealer long, OI=100 -> gex divisor 1_000_000.
        df = _chain(
            [
                (90.0, "put", 0.001, 100, None),    # put gex +1_000
                (95.0, "put", -0.02, 100, None),    # put gex -20_000 (put wall)
                (105.0, "call", 0.002, 100, None),  # call gex +2_000
                (105.0, "put", -0.04, 100, None),   # put gex -40_000
                (110.0, "call", 0.03, 100, None),   # call gex +30_000 (call wall)
            ]
        )
        profile = compute_profile(df, spot=100.0, dealer_position="long")
        self.assertEqual(profile.call_wall, 110.0)
        self.assertEqual(profile.put_wall, 95.0)
        self.assertEqual(
            profile.king_node, {"strike": 105.0, "net_gex": -38000.0}
        )
        by_strike = profile.to_dataframe()
        self.assertEqual(
            list(by_strike.columns), ["strike", "call_gex", "put_gex", "net_gex"]
        )
        self.assertTrue((by_strike["strike"].diff().dropna() > 0).all())

    def test_by_expiry_aggregation(self):
        df = pd.concat(
            [
                _chain([(100.0, "call", 0.05, 100, None)], expiry="2026-10-17"),
                _chain([(100.0, "call", 0.05, 100, None)], expiry="2026-11-20"),
            ],
            ignore_index=True,
        )
        profile = compute_profile(df, spot=100.0)
        by_expiry = profile.by_expiry
        self.assertEqual(list(by_expiry.columns), ["expiry", "net_gex"])
        self.assertEqual(list(by_expiry["expiry"]), ["2026-10-17", "2026-11-20"])
        self.assertAlmostEqual(by_expiry["net_gex"].iloc[0], -50000.0, places=6)
        self.assertAlmostEqual(by_expiry["net_gex"].iloc[1], -50000.0, places=6)

    def test_regime_classification(self):
        pos = compute_profile(
            _chain([(90.0, "call", 0.01, 100, None), (100.0, "call", 0.005, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(pos.regime, "positive")
        neg = compute_profile(
            _chain([(90.0, "call", -0.01, 100, None), (100.0, "call", -0.005, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(neg.regime, "negative")
        mixed = compute_profile(
            _chain([(90.0, "call", 0.01, 100, None), (100.0, "call", -0.01, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(mixed.regime, "mixed")

    def test_gex_velocity(self):
        a = compute_profile(
            _chain([(90.0, "call", 0.005, 100, None), (100.0, "call", 0.01, 100, None)]),
            spot=100.0,
            dealer_position="long",
        )
        b = compute_profile(
            _chain([(100.0, "call", 0.012, 100, None), (110.0, "call", 0.007, 100, None)]),
            spot=100.0,
            dealer_position="long",
        )
        vel = gex_velocity(a, b)
        # divisor 1e6: nets a = {90: 5000, 100: 10000}, b = {100: 12000, 110: 7000}
        self.assertEqual(list(vel.index), [90.0, 100.0, 110.0])
        self.assertAlmostEqual(vel.loc[90.0], -5000.0, places=6)
        self.assertAlmostEqual(vel.loc[100.0], 2000.0, places=6)
        self.assertAlmostEqual(vel.loc[110.0], 7000.0, places=6)

    def test_to_dict_is_json_serializable(self):
        df = _chain([(100.0, "call", 0.05, 100, None)])
        profile = compute_profile(df, spot=100.0)
        d = profile.to_dict()
        json.dumps(d)  # must not raise
        self.assertEqual(d["regime"], "negative")
        self.assertEqual(d["spot"], 100.0)
        self.assertEqual(d["king_node"]["strike"], 100.0)

    def test_empty_dataframe_gives_empty_profile(self):
        df = pd.DataFrame(columns=list(exposure.REQUIRED_COLUMNS))
        profile = compute_profile(df)
        self.assertTrue(profile.by_strike.empty)
        self.assertTrue(profile.by_expiry.empty)
        self.assertIsNone(profile.zero_gamma_flip)
        self.assertIsNone(profile.call_wall)
        self.assertIsNone(profile.put_wall)
        self.assertEqual(profile.king_node, {"strike": None, "net_gex": None})
        self.assertEqual(profile.regime, "mixed")
        self.assertEqual(profile.skipped_rows, 0)
        json.dumps(profile.to_dict())  # NaN spot -> None, must not raise
        self.assertTrue(profile.to_dataframe().empty)


if __name__ == "__main__":
    unittest.main()
