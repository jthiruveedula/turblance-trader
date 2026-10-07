"""Unit tests for the signals package: instinct scoring, IV factors,
suggestion mapping, sweep direction. Stdlib only, no network."""

import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from turblance_trader.signals import (
    atm_iv,
    instinct_score,
    iv_rank,
    label_instinct,
    put_call_skew,
    record_atm_iv,
    suggest,
    sweep_direction,
    term_structure_slope,
)
from turblance_trader.signals.suggestions import directional_confidence


def make_chain(rows):
    """Minimal chain DataFrame in the capture schema shape (subset ok for
    the IV functions, which only need symbol/quote_time/expiry/strike/
    option_type/implied_volatility/spot)."""
    return pd.DataFrame(rows)


def iv_chain():
    rows = []
    for expiry, base in (("2026-09-25", 0.20), ("2026-10-02", 0.23)):
        for strike, otype, bump in (
            (740, "put", 0.04), (750, "put", 0.02), (760, "put", 0.01),
            (760, "call", 0.00), (770, "call", -0.01), (780, "call", -0.02),
        ):
            rows.append({
                "symbol": "SPY", "quote_time": "2026-09-20T15:00:00+00:00",
                "expiry": expiry, "strike": float(strike),
                "option_type": otype, "bid": 1.0, "ask": 1.1, "last": 1.05,
                "implied_volatility": base + bump, "open_interest": 100,
                "volume": 10, "delta": 0.5, "gamma": 0.01, "theta": -0.1,
                "vega": 0.2, "spot": 761.69,
            })
    return make_chain(rows)


class TestInstinctScore(unittest.TestCase):
    def test_bearish_hand_computed(self):
        # S=761.69, no flip, R=-1, walls 770/745, K=760, no sweeps, V=-1
        # r1=-1; r2=0 (|761.69-745|/761.69=2.2% >0.5%; |761.69-770|=1.1% >0.5%)
        # r3=0; r4=-0.5 -> sum=-1.5 -> 100*(-1.5)/3.5 = -42.857 -> -43
        out = instinct_score(761.69, None, 770.0, 745.0, 760.0, -1, [], -1)
        self.assertEqual(out["score"], -43)
        self.assertEqual(out["label"], "BEARISH")
        self.assertEqual(out["components"],
                         {"r1_regime": -1, "r2_walls": 0, "r3_flow": 0,
                          "r4_vex": -0.5})
        # |761.69-760|/761.69 = 0.22% <= 0.3% -> pinned; conviction halved
        # but the score itself is unchanged.
        self.assertTrue(out["pinned"])
        self.assertEqual(out["conviction"], 0.5)
        self.assertTrue(out["experimental"])

    def test_bullish_with_sweeps(self):
        # S=770 above flip 765, at call wall 770 -> r2=-1
        # sweeps (+1,4),(+1,2): 3+2=5 -> r3=+1; V=+1 -> r4=+0.5
        # sum = 1-1+1+0.5 = 1.5 -> 42.857 -> 43 BULLISH
        out = instinct_score(770.0, 765.0, 770.0, 745.0, 800.0, 1,
                             [(1, 4), (1, 2)], 1)
        self.assertEqual(out["score"], 43)
        self.assertEqual(out["label"], "BULLISH")
        self.assertTrue(out["at_resistance"])
        self.assertFalse(out["at_support"])

    def test_pinned_halves_conviction(self):
        out = instinct_score(760.0, None, 770.0, 745.0, 760.0, 1, [], 0)
        # r1=+1 (R>0, no flip), r2=0, r3=0, r4=0 -> 28.57 -> 29 LEAN BULLISH
        self.assertEqual(out["score"], 29)
        self.assertEqual(out["label"], "LEAN BULLISH")
        self.assertTrue(out["pinned"])
        self.assertEqual(out["conviction"], 0.5)

    def test_breadth_capped_at_three(self):
        a = instinct_score(700, None, 800, 600, 750, 1, [(1, 10)], 0)
        b = instinct_score(700, None, 800, 600, 750, 1, [(1, 3)], 0)
        self.assertEqual(a["components"]["r3_flow"], b["components"]["r3_flow"])

    def test_opposing_sweeps_net_out(self):
        out = instinct_score(700, None, 800, 600, 750, 1,
                             [(1, 3), (-1, 3)], 0)
        self.assertEqual(out["components"]["r3_flow"], 0)

    def test_clamp(self):
        out = instinct_score(700, 600.0, 800, 600, 750, 1,
                             [(1, 3), (1, 3), (1, 3)], 1)
        # 1+0+1+0.5=2.5 -> 71.4 -> 71 (within bounds, no clamping needed)
        self.assertEqual(out["score"], 71)
        out2 = instinct_score(700, 800.0, 800, 600, 750, -1,
                              [(-1, 3)] * 10, -1)
        # r1=-1 (below flip), r2=0, r3=-1, r4=-0.5 -> -2.5 -> -71.4 -> -71
        self.assertEqual(out2["score"], -71)

    def test_bad_spot_raises(self):
        with self.assertRaises(ValueError):
            instinct_score(0, None, 1, 1, 1, 1, [], 0)

    def test_label_bands(self):
        self.assertEqual(label_instinct(40), "BULLISH")
        self.assertEqual(label_instinct(39), "LEAN BULLISH")
        self.assertEqual(label_instinct(15), "LEAN BULLISH")
        self.assertEqual(label_instinct(14), "NEUTRAL")
        self.assertEqual(label_instinct(-14), "NEUTRAL")
        self.assertEqual(label_instinct(-15), "LEAN BEARISH")
        self.assertEqual(label_instinct(-39), "LEAN BEARISH")
        self.assertEqual(label_instinct(-40), "BEARISH")
        self.assertEqual(label_instinct(100), "BULLISH")


class TestIVFactors(unittest.TestCase):
    def setUp(self):
        self.df = iv_chain()

    def test_atm_iv(self):
        # front expiry 2026-09-25, strike nearest 761.69 -> 760 call @ 0.20
        self.assertAlmostEqual(atm_iv(self.df, 761.69), 0.20, places=9)

    def test_put_call_skew(self):
        # puts: 0.24,0.22,0.21 -> 0.2233; calls: 0.20,0.19,0.18 -> 0.19
        skew = put_call_skew(self.df, 761.69)
        self.assertAlmostEqual(skew, (0.24 + 0.22 + 0.21) / 3 - 0.19, places=9)

    def test_term_structure_slope(self):
        # second expiry ATM: 760 call @ 0.23; front @ 0.20 -> +0.03
        self.assertAlmostEqual(term_structure_slope(self.df, 761.69), 0.03,
                               places=9)

    def test_single_expiry_slope_none(self):
        df = self.df[self.df["expiry"] == "2026-09-25"]
        self.assertIsNone(term_structure_slope(df, 761.69))

    def test_iv_rank_cold_start(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "atm_iv.json"
            for i in range(5):
                record_atm_iv("SPY", 0.20 + i * 0.001, p,
                             day=f"2026-09-{10 + i:02d}")
            res = iv_rank("SPY", 0.21, p)
            self.assertEqual(res["status"], "insufficient_history")
            self.assertIsNone(res["rank"])
            self.assertEqual(res["n_days"], 5)

    def test_iv_rank_ok(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "atm_iv.json"
            for i in range(20):
                record_atm_iv("SPY", 0.15 + i * 0.01, p,
                             day=f"2026-08-{1 + i:02d}")
            res = iv_rank("SPY", 0.20, p)
            self.assertEqual(res["status"], "ok")
            # values 0.15..0.34; <= 0.20 are 0.15..0.20 -> 6/20 = 30.0
            self.assertAlmostEqual(res["rank"], 30.0)

    def test_iv_factors_plausibility_gate(self):
        from turblance_trader.signals import iv_factors
        # Pre-2026-09-20-fix snapshots carry IVs ~100x too small.
        df = iv_chain().copy()
        df["implied_volatility"] = df["implied_volatility"] / 100.0
        out = iv_factors(df, 761.69)
        self.assertIsNone(out["atm_iv"])
        self.assertIn("iv_note", out)

    def test_iv_factors_ok(self):
        from turblance_trader.signals import iv_factors
        out = iv_factors(iv_chain(), 761.69)
        self.assertAlmostEqual(out["atm_iv"], 0.20, places=9)
        self.assertNotIn("iv_note", out)

    def test_record_refuses_implausible_iv(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "atm_iv.json"
            self.assertFalse(record_atm_iv("SPY", 0.001, p, day="2026-09-20"))
            self.assertFalse(p.exists())

    def test_record_dedupes_by_day(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "atm_iv.json"
            self.assertTrue(record_atm_iv("SPY", 0.20, p, day="2026-09-20"))
            self.assertFalse(record_atm_iv("SPY", 0.21, p, day="2026-09-20"))
            rows = json.loads(p.read_text())["SPY"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["atm_iv"], 0.20)


class TestSweepDirection(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(sweep_direction({"direction": "calls", "side": "lifted"}), 1)
        self.assertEqual(sweep_direction({"direction": "puts", "side": "hit"}), 1)
        self.assertEqual(sweep_direction({"direction": "puts", "side": "lifted"}), -1)
        self.assertEqual(sweep_direction({"direction": "calls", "side": "hit"}), -1)
        self.assertIsNone(sweep_direction({"direction": "calls", "side": "mid"}))
        self.assertIsNone(sweep_direction({"direction": "calls", "side": "zzz"}))


class TestSuggest(unittest.TestCase):
    def _inst(self, score, label, **kw):
        d = {"score": score, "label": label, "pinned": False,
             "at_support": False, "at_resistance": False}
        d.update(kw)
        return d

    def _levels(self, **kw):
        d = {"spot": 761.69, "call_wall": 770.0, "put_wall": 745.0,
             "zero_gamma_flip": None, "king_node": {"strike": 760.0}}
        d.update(kw)
        return d

    def _iv(self):
        return {"atm_iv": 0.20, "put_call_skew": 0.01,
                "term_structure_slope": 0.03}

    def test_quiet(self):
        s = suggest(self._inst(5, "NEUTRAL"), self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "NO_EDGE_QUIET")
        self.assertTrue(s["experimental"])
        self.assertIn("not financial advice", s["disclaimer"])

    def test_pinned(self):
        s = suggest(self._inst(29, "LEAN BULLISH", pinned=True),
                    self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "FAVOR_DEFINED_RISK_PIN")
        self.assertIn("760", s["rationale"])

    def test_resistance(self):
        s = suggest(self._inst(30, "LEAN BULLISH", at_resistance=True),
                    self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "SCALE_EXITS_INTO_RESISTANCE")

    def test_support(self):
        s = suggest(self._inst(20, "LEAN BULLISH", at_support=True),
                    self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "ENTRIES_FAVORED_AT_SUPPORT")

    def test_bullish(self):
        s = suggest(self._inst(55, "BULLISH"), self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "CONSIDER_LONG_EXPOSURE")

    def test_bearish(self):
        s = suggest(self._inst(-55, "BEARISH"), self._iv(), self._levels())
        self.assertEqual(s["suggestion"], "CONSIDER_DOWNSIDE_PROTECTION")

    def test_skew_in_rationale(self):
        iv = self._iv(); iv["put_call_skew"] = 0.05
        s = suggest(self._inst(55, "BULLISH"), iv, self._levels())
        self.assertIn("skew elevated", s["rationale"])

    def test_confidence_low_without_history(self):
        s = suggest(self._inst(55, "BULLISH"), self._iv(), self._levels(),
                    history_days=3)
        self.assertEqual(s["confidence"], "LOW")
        self.assertIn("3/20", s["confidence_reason"])

    def test_confidence_data_driven(self):
        s = suggest(self._inst(55, "BULLISH"), self._iv(), self._levels(),
                    history_days=25)
        self.assertEqual(s["confidence"], "DATA-DRIVEN")

    def test_directional_confidence_gate(self):
        lvl, _ = directional_confidence(19)
        self.assertEqual(lvl, "LOW")
        lvl, _ = directional_confidence(20)
        self.assertEqual(lvl, "DATA-DRIVEN")


if __name__ == "__main__":
    unittest.main()
