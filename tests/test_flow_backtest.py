"""Unit tests for the flow/VEX backtest layer (flow_signals, evaluate
extensions, CLI wiring, reports).

*** SYNTHETIC DATA ONLY ***
Every fixture here is hand-built fake data. These tests verify that the
harness *machinery* works (signal shapes, scoring logic, error paths) —
they prove NOTHING about whether any flow/VEX signal works on real
markets. Real validation requires >= 60 trading days of forward-captured
data; see backtests/reports/flow-volume-burst.md etc.

Stdlib unittest only. No network calls.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import pandas as pd  # noqa: E402

from backtests.harness import evaluate, flow_signals  # noqa: E402

UTC = timezone.utc


class FakeFlowProfile:
    """Duck-typed stand-in for the flow-engine profile contract (synthetic)."""

    def __init__(self, spot=500.0, unusual=(), sweeps=(), cold_start=False):
        self.spot = spot
        self.unusual_strikes = list(unusual)
        self.sweeps = list(sweeps)
        self.cold_start = cold_start
        self.by_strike = pd.DataFrame([
            {"strike": 495.0, "call_volume": 10.0, "put_volume": 5.0,
             "volume_z": 0.5, "oi_change": 0.0, "sweep_score": 0.0,
             "unusual_score": 0.0},
            {"strike": 500.0, "call_volume": 40.0, "put_volume": 30.0,
             "volume_z": 1.0, "oi_change": 10.0, "sweep_score": 2.0,
             "unusual_score": 3.0},
            {"strike": 505.0, "call_volume": 20.0, "put_volume": 8.0,
             "volume_z": 0.2, "oi_change": 0.0, "sweep_score": 0.0,
             "unusual_score": 0.0},
        ])
        self.by_expiry = pd.DataFrame([
            {"expiry": "2026-10-16", "total_volume": 113.0,
             "unusual_score": 3.0, "sweep_score": 2.0},
        ])

    def to_dict(self):
        return {"spot": self.spot, "cold_start": self.cold_start}


def _unusual(strike, option_type, score):
    return {"strike": strike, "option_type": option_type, "score": score,
            "reason": "synthetic", "experimental": True}


def _sweep(direction, side, score):
    return {"expiry": "2026-10-16", "direction": direction, "side": side,
            "strikes_hit": [500.0, 501.0, 502.0], "volume": 3000.0,
            "score": score, "proxy": True, "experimental": True}


class FakeVEXProfile:
    """Duck-typed stand-in for the VEX-engine profile contract (synthetic)."""

    def __init__(self, spot=500.0, vol_regime="positive"):
        self.spot = spot
        self.vol_regime = vol_regime
        self.vega_flip = 502.0
        self.call_vega_wall = 505.0
        self.put_vega_wall = 495.0
        self.vex_king = {"strike": 500.0, "net_vex": -1e7}
        self.by_strike = pd.DataFrame([
            {"strike": 495.0, "call_vex": -1.0, "put_vex": -2.0, "net_vex": -3.0},
            {"strike": 500.0, "call_vex": -5.0, "put_vex": -6.0, "net_vex": -11.0},
            {"strike": 505.0, "call_vex": 4.0, "put_vex": 1.0, "net_vex": 5.0},
        ])
        self.by_expiry = pd.DataFrame()

    def to_dict(self):
        return {"spot": self.spot, "vol_regime": self.vol_regime}


class FlowSignalShapeTest(unittest.TestCase):
    """Each flow/vex signal returns the documented shape and experimental flag."""

    def test_burst_bullish_on_call_cluster_above_spot(self):
        p = FakeFlowProfile(spot=500.0, unusual=[
            _unusual(505.0, "call", 4.0),
            _unusual(510.0, "call", 3.5),
            _unusual(495.0, "put", 2.5),
        ])
        out = flow_signals.unusual_volume_burst(p)
        self.assertEqual(out["signal"], "unusual_volume_burst")
        self.assertTrue(out["experimental"])
        self.assertEqual(out["bias"], "bullish")
        self.assertEqual(out["levels"], [505.0, 510.0])
        self.assertIn("rationale", out)
        self.assertIn("details", out)

    def test_burst_bearish_on_put_cluster_below_spot(self):
        p = FakeFlowProfile(spot=500.0, unusual=[
            _unusual(490.0, "put", 4.2),
            _unusual(485.0, "put", 3.1),
        ])
        self.assertEqual(
            flow_signals.unusual_volume_burst(p)["bias"], "bearish")

    def test_burst_neutral_on_wrong_side_cluster(self):
        # Calls below spot carry no directional meaning -> neutral.
        p = FakeFlowProfile(spot=500.0, unusual=[
            _unusual(490.0, "call", 4.0),
        ])
        out = flow_signals.unusual_volume_burst(p)
        self.assertEqual(out["bias"], "neutral")
        self.assertTrue(out["experimental"])

    def test_burst_cold_start_is_neutral_by_design(self):
        p = FakeFlowProfile(spot=500.0, cold_start=True, unusual=[
            _unusual(505.0, "call", 4.0),
        ])
        out = flow_signals.unusual_volume_burst(p)
        self.assertEqual(out["bias"], "neutral")
        self.assertIn("cold-start", out["rationale"])
        self.assertTrue(out["details"]["cold_start"])

    def test_burst_empty_is_neutral(self):
        out = flow_signals.unusual_volume_burst(FakeFlowProfile())
        self.assertEqual(out["bias"], "neutral")

    def test_sweep_lifted_calls_bullish_hit_puts_bearish(self):
        p = FakeFlowProfile(sweeps=[_sweep("calls", "lifted", 9.0)])
        out = flow_signals.sweep_followthrough(p)
        self.assertEqual(out["bias"], "bullish")
        self.assertTrue(out["details"]["proxy"])
        self.assertIn("UNCONFIRMED PROXY", out["rationale"])

        p2 = FakeFlowProfile(sweeps=[_sweep("puts", "lifted", 9.0)])
        self.assertEqual(
            flow_signals.sweep_followthrough(p2)["bias"], "bearish")
        p3 = FakeFlowProfile(sweeps=[_sweep("calls", "hit", 9.0)])
        self.assertEqual(
            flow_signals.sweep_followthrough(p3)["bias"], "bearish")
        p4 = FakeFlowProfile(sweeps=[_sweep("puts", "hit", 9.0)])
        self.assertEqual(
            flow_signals.sweep_followthrough(p4)["bias"], "bullish")

    def test_sweep_no_events_neutral(self):
        out = flow_signals.sweep_followthrough(FakeFlowProfile())
        self.assertEqual(out["bias"], "neutral")
        self.assertTrue(out["experimental"])

    def test_vex_regime_mapping(self):
        self.assertEqual(
            flow_signals.vex_regime(FakeVEXProfile(vol_regime="positive"))["bias"],
            "contraction")
        self.assertEqual(
            flow_signals.vex_regime(FakeVEXProfile(vol_regime="negative"))["bias"],
            "expansion")
        self.assertEqual(
            flow_signals.vex_regime(FakeVEXProfile(vol_regime="mixed"))["bias"],
            "uncertain")
        out = flow_signals.vex_regime(FakeVEXProfile(vol_regime="positive"))
        self.assertTrue(out["experimental"])
        self.assertIn("arbitrary heuristics", out["rationale"])
        self.assertIn(502.0, out["levels"])  # vega flip included

    def test_vex_regime_unknown(self):
        p = FakeVEXProfile()
        p.vol_regime = "sideways"
        out = flow_signals.vex_regime(p)
        self.assertEqual(out["bias"], "unknown")

    def test_darkpool_divergence_shapes(self):
        ats = pd.DataFrame([
            {"week_start": "2026-08-24", "symbol": "SPY", "ats_mpid": "AQUA",
             "ats_name": None, "tier": "T1", "weekly_shares": 1_000_000,
             "weekly_trades": 100, "block_bucket": None,
             "last_updated": None, "source": "synthetic"},
            {"week_start": "2026-08-31", "symbol": "SPY", "ats_mpid": "AQUA",
             "ats_name": None, "tier": "T1", "weekly_shares": 1_500_000,
             "weekly_trades": 140, "block_bucket": None,
             "last_updated": None, "source": "synthetic"},
        ])
        up = flow_signals.darkpool_divergence(
            ats, weekly_prices=[("2026-08-24", 600.0), ("2026-08-31", 610.0)])
        self.assertEqual(up["bias"], "distribution")  # ATS up + price up
        self.assertTrue(up["experimental"])

        down = flow_signals.darkpool_divergence(
            ats, weekly_prices=[("2026-08-24", 600.0), ("2026-08-31", 590.0)])
        self.assertEqual(down["bias"], "accumulation")  # ATS up + price down

        nodata = flow_signals.darkpool_divergence(
            pd.DataFrame(columns=["week_start", "symbol", "weekly_shares"]))
        self.assertEqual(nodata["bias"], "neutral")

    def test_run_all_and_new_context(self):
        ctx = flow_signals.new_context()
        outs = flow_signals.run_all(
            flow_profile=FakeFlowProfile(unusual=[_unusual(505.0, "call", 4.0)]),
            vex_profile=FakeVEXProfile(),
            context=ctx)
        self.assertEqual(len(outs), 3)
        self.assertEqual(
            [o["signal"] for o in outs],
            ["unusual_volume_burst", "sweep_followthrough", "vex_regime"])
        self.assertTrue(all(o["experimental"] for o in outs))
        # run_all with nothing supplied fabricates nothing.
        self.assertEqual(flow_signals.run_all(), [])


def _flow_session(date, profile, closes, vex_profile=None):
    t0 = datetime(2026, 9, 21, 14, 30, tzinfo=UTC)
    prices = [(t0 + timedelta(minutes=5 * i), c)
              for i, c in enumerate(closes)]
    s = {"date": date, "profiles": [(t0, profile)], "prices": prices,
         "flow_profile": profile}
    if vex_profile is not None:
        s["vex_profile"] = vex_profile
    return s


class FlowEvaluateTest(unittest.TestCase):
    """evaluate.py flow/vex metrics on hand-built synthetic scenarios."""

    def test_unusual_burst_hit_rate(self):
        # 7 sessions, rising closes; first two carry bullish bursts.
        bull = FakeFlowProfile(spot=500.0, unusual=[
            _unusual(505.0, "call", 4.0), _unusual(510.0, "call", 3.5)])
        flat = FakeFlowProfile()
        sessions = [
            _flow_session(f"2026-09-{21 + i:02d}",
                          bull if i < 2 else flat,
                          [100.0 + i, 100.2 + i])
            for i in range(7)
        ]
        res = evaluate.unusual_burst_hit_rate(sessions, n_sessions_ahead=5)
        self.assertEqual(res["n_scored"], 2)
        self.assertEqual(res["hits"], 2)
        self.assertAlmostEqual(res["hit_rate"], 1.0)
        self.assertIn("wilson_95ci", res)
        self.assertIn("in_sample", res)
        self.assertIn("out_of_sample", res)
        self.assertIn("note", res["costs"])
        self.assertIn("UNVALIDATED", res["status"])

    def test_sweep_followthrough_rate(self):
        sweeps = FakeFlowProfile(sweeps=[_sweep("calls", "lifted", 9.0)])
        flat = FakeFlowProfile()
        sessions = [
            _flow_session(f"2026-09-{21 + i:02d}",
                          sweeps if i == 0 else flat,
                          [100.0 + i, 100.2 + i])
            for i in range(7)
        ]
        res = evaluate.sweep_followthrough_rate(sessions, n_sessions_ahead=5)
        self.assertEqual(res["n_scored"], 1)
        self.assertEqual(res["hits"], 1)
        self.assertAlmostEqual(res["followthrough_rate"], 1.0)
        self.assertTrue(res["per_session"][0]["event_proxy"])

    def test_vex_regime_iv_accuracy_structure(self):
        regimes = ["positive", "negative", "positive", "negative",
                   "mixed", "positive"]
        closes = [100.0, 100.5, 99.5, 103.0, 97.0, 104.0]
        sessions = [
            _flow_session(f"2026-09-{21 + i:02d}", FakeFlowProfile(),
                          [closes[i]], vex_profile=FakeVEXProfile(
                              vol_regime=regimes[i]))
            for i in range(6)
        ]
        res = evaluate.vex_regime_iv_accuracy(sessions, n_sessions_ahead=2)
        self.assertGreater(res["n_scored"], 0)
        self.assertIn("accuracy", res)
        self.assertIn("wilson_95ci", res)
        self.assertIn("PROXY", res["iv_proxy"])
        self.assertIn("in_sample", res)
        self.assertIn("out_of_sample", res)

    def test_empty_and_unscoreable_raise(self):
        for fn in (evaluate.unusual_burst_hit_rate,
                   evaluate.sweep_followthrough_rate,
                   evaluate.vex_regime_iv_accuracy,
                   evaluate.run_all_flow):
            with self.assertRaises(evaluate.InsufficientDataError):
                fn([])
        # All-neutral sessions: nothing to score -> honest error, not zeros.
        flat = [_flow_session("2026-09-21", FakeFlowProfile(), [100.0, 100.1])]
        with self.assertRaises(evaluate.InsufficientDataError):
            evaluate.unusual_burst_hit_rate(flat)
        with self.assertRaises(evaluate.InsufficientDataError):
            evaluate.sweep_followthrough_rate(flat)

    def test_run_all_flow_combines_metrics(self):
        bull = FakeFlowProfile(spot=500.0, unusual=[
            _unusual(505.0, "call", 4.0)])
        bull.sweeps = [_sweep("calls", "lifted", 9.0)]
        sessions = [
            _flow_session(f"2026-09-{21 + i:02d}", bull,
                          [100.0 + i], vex_profile=FakeVEXProfile(
                              vol_regime="positive" if i % 2 else "negative"))
            for i in range(8)
        ]
        res = evaluate.run_all_flow(sessions, n_sessions_ahead=2)
        self.assertIn("unusual_burst", res)
        self.assertIn("sweep_followthrough", res)
        self.assertIn("vex_regime", res)
        self.assertEqual(res["n_sessions"], 8)


class FlowRunValidationCLITest(unittest.TestCase):
    """The CLI exits 2 for --module flow/vex with no data, like gex."""

    def test_flow_and_vex_exit_2_without_data(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "backtests", "harness"))
        import run_validation
        tmp = tempfile.mkdtemp()
        try:
            for module in ("flow", "vex"):
                with self.assertRaises(SystemExit) as cm:
                    run_validation.main([
                        "--module", module, "--symbol", "SPY",
                        "--data-root", os.path.join(tmp, "empty")])
                self.assertEqual(cm.exception.code, 2,
                                 f"module={module}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FlowReportFilesTest(unittest.TestCase):
    """The three flow/vex reports exist and meet the evidence standard."""

    REPORTS = ("flow-volume-burst.md", "flow-sweep-followthrough.md",
               "vex-regime.md")

    def test_reports_exist_with_evidence_standard(self):
        for fname in self.REPORTS:
            path = os.path.join(REPO_ROOT, "backtests", "reports", fname)
            self.assertTrue(os.path.isfile(path), fname)
            text = open(path).read()
            for needle in ("UNVALIDATED — EXPERIMENTAL", "## Hypothesis",
                           "## Honest limitations", "<!-- RESULTS:START -->",
                           "<!-- RESULTS:END -->", "## Re-validation trigger"):
                self.assertIn(needle, text, f"{fname}: {needle}")
            # Zero fabricated numbers: no percentage figures in Results.
            results = text.split("<!-- RESULTS:START -->")[1].split(
                "<!-- RESULTS:END -->")[0]
            self.assertNotIn("%", results.replace("95%", ""),
                             f"{fname}: Results section must contain no statistics")


if __name__ == "__main__":
    unittest.main()
