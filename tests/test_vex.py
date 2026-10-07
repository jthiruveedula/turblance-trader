"""Unit tests for the turblance-trader VEX engine (stdlib unittest, no network).

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

from turblance_trader.vex import VEXProfile, compute_vex_profile, vex_velocity
from turblance_trader.vex import exposure
from turblance_trader.gex import black_scholes


def _chain(rows, spot=100.0, quote_time="2026-09-18T15:30:00Z", expiry="2026-10-17"):
    """Build a chain DataFrame from compact row specs.

    Each row: (strike, option_type, vega, open_interest, implied_volatility).
    vega/implied_volatility may be None to mean NaN. Vega is quoted per
    1.0 (100%) IV move, per the source convention.
    """
    records = []
    for strike, option_type, vg, oi, iv in rows:
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
                "gamma": 0.01,
                "theta": -0.1,
                "vega": float("nan") if vg is None else float(vg),
                "spot": float(spot),
            }
        )
    return pd.DataFrame(records)


class TestBSVega(unittest.TestCase):
    def test_vega_atm_forward_identity(self):
        # S=K=100, sigma=0.2, T=1, r=-sigma^2/2 gives d1 = 0 exactly, so
        # vega must equal S * N'(0) * sqrt(T) = S / sqrt(2*pi) per 1.0 IV.
        s, k, sigma, t, r = 100.0, 100.0, 0.2, 1.0, -0.02
        got = black_scholes.vega(s, k, t, sigma, risk_free_rate=r)
        expected = s / math.sqrt(2.0 * math.pi)
        self.assertAlmostEqual(got, expected, places=12)
        self.assertAlmostEqual(got, 39.89422804014327, places=12)

    def test_vega_is_positive_and_finite(self):
        v = black_scholes.vega(100.0, 95.0, 0.5, 0.25)
        self.assertTrue(math.isfinite(v) and v > 0.0)

    def test_vega_degenerate_inputs_give_finite_zero(self):
        # T <= 0, sigma <= 0, bad prices -> vega 0, never NaN, never raises.
        for kwargs in (
            dict(time_to_expiry=0.0, volatility=0.2),
            dict(time_to_expiry=-1.0, volatility=0.2),
            dict(time_to_expiry=1.0, volatility=0.0),
            dict(time_to_expiry=1.0, volatility=-0.5),
            dict(time_to_expiry=float("nan"), volatility=0.2),
        ):
            v = black_scholes.vega(100.0, 100.0, kwargs["time_to_expiry"], kwargs["volatility"])
            self.assertEqual(v, 0.0, f"expected 0 for {kwargs}")
            self.assertTrue(math.isfinite(v))
        self.assertEqual(black_scholes.vega(0.0, 100.0, 1.0, 0.2), 0.0)
        self.assertEqual(black_scholes.vega(100.0, -5.0, 1.0, 0.2), 0.0)


class TestExposure(unittest.TestCase):
    def test_single_contract_vex_matches_hand_computation(self):
        # UNIT CONVENTION CHECK: vega=0.5 per 1.0 (100%) IV move, OI=200.
        #   per-1pt vega = 0.5 / 100 = 0.005 $/share per 1pt IV
        #   customer_vex = 0.005 * 200 * 100 = $100 per 1 vol point
        df = _chain([(100.0, "call", 0.5, 200, None)])
        out, meta = exposure.compute_exposure(df)
        self.assertEqual(meta["skipped_rows"], 0)
        self.assertAlmostEqual(out["resolved_vega"].iloc[0], 0.005, places=12)
        self.assertAlmostEqual(out["dealer_vex"].iloc[0], -100.0, places=9)

        for position, expected in (("short", -100.0), ("long", 100.0), ("flat", 0.0)):
            profile = compute_vex_profile(df, spot=100.0, dealer_position=position)
            row = profile.by_strike.iloc[0]
            self.assertAlmostEqual(row["call_vex"], expected, places=9)
            self.assertAlmostEqual(row["put_vex"], 0.0, places=9)
            self.assertAlmostEqual(row["net_vex"], expected, places=9)

    def test_dealer_position_must_be_valid(self):
        df = _chain([(100.0, "call", 0.5, 200, None)])
        with self.assertRaises(ValueError):
            compute_vex_profile(df, dealer_position="sideways")

    def test_missing_vega_falls_back_to_black_scholes(self):
        # Row A: supplied vega equal to the BS vega of row B's IV.
        # Row B: vega NaN, IV 0.2 -> BS vega with T = 29/365 years.
        t = 29.0 / 365.0
        bs_v = black_scholes.vega(100.0, 100.0, t, 0.2, risk_free_rate=0.0)
        self.assertGreater(bs_v, 0.0)
        supplied = _chain([(100.0, "call", bs_v, 100, None)])
        fallback = _chain([(100.0, "call", None, 100, 0.2)])
        out_s, _ = exposure.compute_exposure(supplied)
        out_f, meta = exposure.compute_exposure(fallback)
        self.assertEqual(meta["skipped_rows"], 0)
        # Fallback vega resolved to BS vega / 100 (per-1pt basis).
        self.assertAlmostEqual(out_f["resolved_vega"].iloc[0], bs_v / 100.0, places=12)
        # Both paths land on the same dollar exposure: no silent unit mixing.
        self.assertAlmostEqual(
            out_s["dealer_vex"].iloc[0], out_f["dealer_vex"].iloc[0], places=6
        )
        expected = -1.0 * (bs_v / 100.0) * 100 * 100.0
        self.assertAlmostEqual(out_f["dealer_vex"].iloc[0], expected, places=6)

    def test_rows_missing_both_vega_and_iv_are_skipped_and_counted(self):
        df = _chain(
            [
                (100.0, "call", 0.5, 200, None),  # usable
                (105.0, "put", None, 100, None),  # skipped
                (110.0, "call", None, 100, None),  # skipped
            ]
        )
        profile = compute_vex_profile(df, spot=100.0)
        self.assertEqual(profile.skipped_rows, 2)
        self.assertEqual(profile.n_rows, 3)
        self.assertEqual(list(profile.by_strike["strike"]), [100.0])
        self.assertIn("skipped_rows", profile.to_dict())

    def test_zero_time_and_zero_vol_fallbacks_are_finite_zeros(self):
        # Same-day expiry -> T = 0; IV = 0 -> sigma = 0. Both must resolve to
        # vega 0 with no NaN leaking into exposure.
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
        self.assertTrue((out["resolved_vega"] == 0.0).all())
        self.assertTrue((out["dealer_vex"] == 0.0).all())
        self.assertTrue(out["resolved_vega"].notna().all())
        self.assertTrue(out["dealer_vex"].notna().all())

    def test_source_vega_iv_unit_must_be_positive(self):
        df = _chain([(100.0, "call", 0.5, 200, None)])
        with self.assertRaises(ValueError):
            compute_vex_profile(df, source_vega_iv_unit=0.0)
        with self.assertRaises(ValueError):
            compute_vex_profile(df, source_vega_iv_unit=-1.0)

    def test_per_vol_point_feed_needs_no_division(self):
        # A feed quoting vega per 1 vol point already: source_vega_iv_unit=0.01
        # cancels the /100 divisor. vega=0.5 per 1pt, OI=200 -> customer 10_000.
        df = _chain([(100.0, "call", 0.5, 200, None)])
        profile = compute_vex_profile(df, spot=100.0, source_vega_iv_unit=0.01)
        self.assertAlmostEqual(profile.by_strike["net_vex"].iloc[0], -10000.0, places=6)

    def test_dealer_position_flipping(self):
        # Same chain, two conventions: net VEX must negate exactly.
        df = _chain(
            [
                (95.0, "call", 4.0, 100, None),
                (105.0, "put", 6.0, 100, None),
            ]
        )
        short = compute_vex_profile(df, spot=100.0, dealer_position="short")
        long = compute_vex_profile(df, spot=100.0, dealer_position="long")
        value_cols = ["call_vex", "put_vex", "net_vex"]
        pd.testing.assert_frame_equal(
            short.by_strike[value_cols],
            -long.by_strike[value_cols],
            check_dtype=False,
        )
        pd.testing.assert_frame_equal(
            short.by_strike[["strike"]],
            long.by_strike[["strike"]],
            check_dtype=False,
        )
        self.assertAlmostEqual(
            short.by_expiry["net_vex"].sum(),
            -long.by_expiry["net_vex"].sum(),
            places=9,
        )
        self.assertEqual(short.vol_regime, "negative")
        self.assertEqual(long.vol_regime, "positive")


class TestLevels(unittest.TestCase):
    def test_vega_flip_interpolation(self):
        # dealer_position='long' keeps our synthetic signs intact.
        # OI=100, vega per 1.0 IV: net_vex = vega * 100 (dealer long).
        df = _chain(
            [
                (90.0, "call", 10.0, 100, None),   # net +1_000
                (100.0, "call", -10.0, 100, None),  # net -1_000
            ],
            spot=95.0,
        )
        profile = compute_vex_profile(df, spot=95.0, dealer_position="long")
        self.assertAlmostEqual(profile.vega_flip, 95.0, places=9)

    def test_vega_flip_none_without_crossing(self):
        df = _chain(
            [
                (90.0, "call", 1.0, 100, None),
                (100.0, "call", 2.0, 100, None),
            ]
        )
        profile = compute_vex_profile(df, dealer_position="long")
        self.assertIsNone(profile.vega_flip)

    def test_vega_flip_ignores_zero_exposure_tails(self):
        # Regression: far-tail strikes with no open interest carry zero VEX.
        # A leading run of zero-exposure strikes must not anchor the flip;
        # uniformly negative exposure elsewhere means no genuine crossing.
        df = _chain(
            [
                (50.0, "call", 1.0, 0, None),
                (55.0, "call", 1.0, 0, None),
                (90.0, "call", -1.0, 100, None),
                (100.0, "call", -2.0, 100, None),
            ],
            spot=95.0,
        )
        profile = compute_vex_profile(df, spot=95.0, dealer_position="long")
        self.assertIsNone(profile.vega_flip)

    def test_vega_flip_with_leading_zero_tail_and_genuine_crossing(self):
        # The zero tail must not move the crossing: nets -100 @90, +100 @100
        # still interpolate to exactly 95.
        df = _chain(
            [
                (50.0, "call", 1.0, 0, None),
                (90.0, "call", -1.0, 100, None),
                (100.0, "call", 1.0, 100, None),
            ],
            spot=95.0,
        )
        profile = compute_vex_profile(df, spot=95.0, dealer_position="long")
        self.assertAlmostEqual(profile.vega_flip, 95.0, places=9)

    def test_walls_and_vex_king(self):
        # spot=100, dealer long, OI=100 -> net_vex = vega * 100.
        df = _chain(
            [
                (90.0, "put", 1.0, 100, None),    # put vex +100
                (95.0, "put", -2.0, 100, None),   # put vex -200 (put wall)
                (105.0, "call", 0.5, 100, None),  # call vex +50
                (105.0, "put", -4.0, 100, None),  # put vex -400, net -350
                (110.0, "call", 3.0, 100, None),  # call vex +300 (call wall)
            ]
        )
        profile = compute_vex_profile(df, spot=100.0, dealer_position="long")
        self.assertEqual(profile.call_vega_wall, 110.0)
        self.assertEqual(profile.put_vega_wall, 95.0)
        self.assertEqual(
            profile.vex_king, {"strike": 105.0, "net_vex": -350.0}
        )
        by_strike = profile.to_dataframe()
        self.assertEqual(
            list(by_strike.columns), ["strike", "call_vex", "put_vex", "net_vex"]
        )
        self.assertTrue((by_strike["strike"].diff().dropna() > 0).all())

    def test_by_expiry_aggregation(self):
        df = pd.concat(
            [
                _chain([(100.0, "call", 0.5, 200, None)], expiry="2026-10-17"),
                _chain([(100.0, "call", 0.5, 200, None)], expiry="2026-11-20"),
            ],
            ignore_index=True,
        )
        profile = compute_vex_profile(df, spot=100.0)
        by_expiry = profile.by_expiry
        self.assertEqual(list(by_expiry.columns), ["expiry", "net_vex"])
        self.assertEqual(list(by_expiry["expiry"]), ["2026-10-17", "2026-11-20"])
        self.assertAlmostEqual(by_expiry["net_vex"].iloc[0], -100.0, places=6)
        self.assertAlmostEqual(by_expiry["net_vex"].iloc[1], -100.0, places=6)

    def test_vol_regime_classification(self):
        pos = compute_vex_profile(
            _chain([(90.0, "call", 1.0, 100, None), (100.0, "call", 0.5, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(pos.vol_regime, "positive")
        neg = compute_vex_profile(
            _chain([(90.0, "call", -1.0, 100, None), (100.0, "call", -0.5, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(neg.vol_regime, "negative")
        mixed = compute_vex_profile(
            _chain([(90.0, "call", 1.0, 100, None), (100.0, "call", -1.0, 100, None)]),
            dealer_position="long",
        )
        self.assertEqual(mixed.vol_regime, "mixed")

    def test_vex_velocity(self):
        a = compute_vex_profile(
            _chain([(90.0, "call", 5.0, 100, None), (100.0, "call", 10.0, 100, None)]),
            spot=100.0,
            dealer_position="long",
        )
        b = compute_vex_profile(
            _chain([(100.0, "call", 12.0, 100, None), (110.0, "call", 7.0, 100, None)]),
            spot=100.0,
            dealer_position="long",
        )
        vel = vex_velocity(a, b)
        # nets a = {90: 500, 100: 1000}, b = {100: 1200, 110: 700}
        self.assertEqual(list(vel.index), [90.0, 100.0, 110.0])
        self.assertAlmostEqual(vel.loc[90.0], -500.0, places=6)
        self.assertAlmostEqual(vel.loc[100.0], 200.0, places=6)
        self.assertAlmostEqual(vel.loc[110.0], 700.0, places=6)

    def test_to_dict_is_json_serializable(self):
        df = _chain([(100.0, "call", 0.5, 200, None)])
        profile = compute_vex_profile(df, spot=100.0)
        d = profile.to_dict()
        json.dumps(d)  # must not raise
        self.assertEqual(d["vol_regime"], "negative")
        self.assertEqual(d["spot"], 100.0)
        self.assertEqual(d["vex_king"]["strike"], 100.0)
        self.assertEqual(d["dealer_position"], "short")
        self.assertEqual(d["total_net_vex"], -100.0)

    def test_empty_dataframe_gives_empty_profile(self):
        df = pd.DataFrame(columns=list(exposure.REQUIRED_COLUMNS))
        profile = compute_vex_profile(df)
        self.assertTrue(profile.by_strike.empty)
        self.assertTrue(profile.by_expiry.empty)
        self.assertIsNone(profile.vega_flip)
        self.assertIsNone(profile.call_vega_wall)
        self.assertIsNone(profile.put_vega_wall)
        self.assertEqual(profile.vex_king, {"strike": None, "net_vex": None})
        self.assertEqual(profile.vol_regime, "mixed")
        self.assertEqual(profile.skipped_rows, 0)
        json.dumps(profile.to_dict())  # NaN spot -> None, must not raise
        self.assertTrue(profile.to_dataframe().empty)


if __name__ == "__main__":
    unittest.main()
