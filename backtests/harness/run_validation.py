#!/usr/bin/env python3
"""Run the full GEX-signal validation and refresh the validation reports.

Usage:
    python3 backtests/harness/run_validation.py --symbol SPY [--min-days 60]

What it does (when data exists):
  1. Loads forward-captured snapshots via the harness loader.
  2. Builds per-snapshot GEX profiles with the GEX engine
     (turblance_trader.gex.compute_profile).
  3. Groups snapshots into daily sessions with intraday price series taken
     from the snapshots' spot column.
  4. Runs harness.evaluate.run_all and writes the Results section of each
     report in backtests/reports/ (between the RESULTS markers).

Right now forward capture has only just started, so this exits with a clear
"insufficient data" message (exit code 2) instead of fabricating results.

Exit codes: 0 = validation ran, reports updated; 2 = insufficient data;
3 = GEX engine not importable yet.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))  # package lives in src/, not pip-installed

from backtests.harness import evaluate, loader  # noqa: E402

REPORTS_DIR = REPO_ROOT / "backtests" / "reports"
REPORT_KEYS = {
    "king_magnet": "gex-king-magnet.md",
    "tap_decay": "gex-tap-decay.md",
    "regime": "gex-regime.md",
}
FLOW_REPORT_KEYS = {
    "unusual_burst": "flow-volume-burst.md",
    "sweep_followthrough": "flow-sweep-followthrough.md",
}
VEX_REPORT_KEYS = {
    "vex_regime": "vex-regime.md",
}
MIN_TRADING_DAYS = 60
MODULES = ("gex", "flow", "vex")


def die(msg: str, code: int):
    print(f"run_validation: {msg}", file=sys.stderr)
    sys.exit(code)


def _check_gex_engine():
    try:
        from turblance_trader.gex import compute_profile  # noqa: F401
    except Exception as exc:  # engine sibling still building it
        die(
            "GEX engine not importable yet (turblance_trader.gex). "
            f"Import error: {exc}. Validation cannot run without profiles.",
            3,
        )


def _build_sessions(symbol: str, snapshots):
    """Group snapshots into daily sessions.

    Profiles are computed with the GEX engine; the intraday price series is
    the snapshots' own spot column (no external price feed needed).
    """
    from turblance_trader.gex import compute_profile

    sessions = {}
    for quote_time, df in snapshots:
        day = quote_time.date().isoformat()
        profile = compute_profile(df)
        spot = float(df["spot"].iloc[-1])
        sessions.setdefault(day, {"date": day, "profiles": [], "prices": []})
        sessions[day]["profiles"].append((quote_time, profile))
        sessions[day]["prices"].append((quote_time, spot))
    out = []
    for day in sorted(sessions):
        s = sessions[day]
        s["profiles"].sort(key=lambda x: x[0])
        s["prices"].sort(key=lambda x: x[0])
        s["regime"] = getattr(s["profiles"][-1][1], "regime", "unknown")
        out.append(s)
    return out


def _build_flow_vex_sessions(symbol: str, snapshots):
    """Group snapshots into daily sessions carrying flow + VEX profiles.

    Flow baselines are built PROGRESSIVELY (no peeking): each day's flow
    profile uses only snapshots from *earlier* days, so day 1..N of capture
    are cold-start by design — baselines accrue as history grows.
    """
    from turblance_trader.flow import compute_flow_profile
    from turblance_trader.flow.unusual_activity import compute_baselines
    from turblance_trader.vex import compute_vex_profile

    import pandas as pd

    sessions = {}
    day_frames = {}
    for quote_time, df in snapshots:
        day = quote_time.date().isoformat()
        sessions.setdefault(day, {"date": day, "profiles": [], "prices": [],
                                  "flow_profile": None, "vex_profile": None})
        sessions[day]["profiles"].append((quote_time, df))
        day_frames.setdefault(day, []).append((quote_time, df))

    history = []
    out = []
    for day in sorted(sessions):
        s = sessions[day]
        frames = sorted(day_frames[day], key=lambda x: x[0])
        s["prices"] = [(qt, float(df["spot"].iloc[-1])) for qt, df in frames]
        first_qt, first_df = frames[0]
        baselines = compute_baselines(
            pd.concat([h for _, h in history]) if history else None)
        prev_df = history[-1][1] if history else None
        flow_profile = compute_flow_profile(
            first_df, prev_df=prev_df, baselines=baselines)
        vex_profile = compute_vex_profile(first_df)
        s["flow_profile"] = flow_profile
        s["vex_profile"] = vex_profile
        s["profiles"] = [(first_qt, flow_profile)]
        s["regime"] = getattr(vex_profile, "vol_regime", "unknown")
        history.extend(frames)
        out.append(s)
    return out


def _render_results(key: str, results: dict) -> str:
    today = date.today().isoformat()
    if key == "king_magnet":
        m = results["king_magnet"]
        body = (
            f"Sessions scored: {m['n_scored']} (of {m['n_sessions']} loaded)\n"
            f"King-node hit rate: {m['hit_rate'] if m['hit_rate'] is not None else 'n/a'}\n"
            f"95% Wilson CI: {m['wilson_95ci']}\n"
            f"Touch tolerance: {m['tol_pct']}%\n"
        )
    elif key == "tap_decay":
        m = results["tap_decay"]
        lines = ["| Touch # | Touches | Reactions | Observed | Predicted | 95% CI |",
                 "|---|---|---|---|---|---|"]
        for k in (1, 2, 3, "4+"):
            b = m["buckets"][k]
            obs = f"{b['observed_rate']:.2%}" if b["observed_rate"] is not None else "n/a"
            lines.append(
                f"| {k} | {b['touches']} | {b['reactions']} | {obs} | "
                f"{b['predicted_rate']:.0%} | {b['wilson_95ci']} |"
            )
        body = "\n".join(lines)
    else:
        m = results["regime"]
        body = (
            f"Sessions scored: {m['n_scored']} (mixed/unscored: {m['n_mixed_unscored']})\n"
            f"Forecast accuracy: {m['accuracy']:.2%} (95% Wilson CI: {m['wilson_95ci']})\n"
            f"Mean range positive-gamma days: {m['mean_range_pct_positive']}\n"
            f"Mean range negative-gamma days: {m['mean_range_pct_negative']}\n"
            f"Direction match (pos < neg): {m['direction_match']}\n"
        )
    return (
        f"Status: VALIDATED on real forward-captured data (run {today}).\n\n"
        f"{body}\n\n"
        f"Costs assumptions: {results['costs']['note']}\n"
    )


def _render_flow_results(key: str, results: dict) -> str:
    today = date.today().isoformat()
    m = results[key]
    ci = m["wilson_95ci"]
    if key in ("unusual_burst", "sweep_followthrough"):
        rate_key = "hit_rate" if key == "unusual_burst" else "followthrough_rate"
        rate = m[rate_key]
        label = "Hit rate" if key == "unusual_burst" else "Follow-through rate"
        body = (
            f"Sessions scored: {m['n_scored']} (neutral/unscored: "
            f"{m['n_neutral_unscored']}, no forward window: "
            f"{m['n_no_forward_window']})\n"
            f"{label}: {rate if rate is not None else 'n/a'}\n"
            f"95% Wilson CI: {ci}\n"
            f"Forward window: {m['n_sessions_ahead']} sessions\n"
        )
    else:  # vex_regime
        body = (
            f"Sessions scored: {m['n_scored']} (mixed/unscored: {m['n_mixed_unscored']})\n"
            f"Forecast accuracy: {m['accuracy'] if m['accuracy'] is not None else 'n/a'} "
            f"(95% Wilson CI: {ci})\n"
            f"IV proxy: {m['iv_proxy']}\n"
            f"Mean forward RV positive-regime days: {m['mean_fwd_rv_positive']}\n"
            f"Mean forward RV negative-regime days: {m['mean_fwd_rv_negative']}\n"
            f"Direction match (pos < neg): {m['direction_match']}\n"
        )
    in_s, out_s = m.get("in_sample"), m.get("out_of_sample")
    split = ""
    if in_s is not None or out_s is not None:
        def _r(x):
            if x is None or x.get("n_scored") == 0:
                return "n/a"
            v = x.get("hit_rate", x.get("followthrough_rate", x.get("accuracy")))
            return f"{v:.2%}" if v is not None else "n/a"
        split = (
            f"\nIn-sample (first {m.get('in_sample_frac', 0.6):.0%} of days): "
            f"{_r(in_s)} over {(in_s or {}).get('n_scored', 0)} sessions\n"
            f"Out-of-sample: {_r(out_s)} over {(out_s or {}).get('n_scored', 0)} sessions\n"
        )
    return (
        f"Status: VALIDATED on real forward-captured data (run {today}).\n\n"
        f"{body}{split}\n"
        f"Costs assumptions: {results['costs']['note']}\n"
    )


def _update_report(path: Path, key: str, results: dict,
                   renderer=_render_results) -> None:
    text = path.read_text()
    start, end = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
    if start not in text or end not in text:
        die(f"Report {path.name} is missing its RESULTS markers.", 1)
    new_results = renderer(key, results)
    text = re.sub(
        re.escape(start) + r".*?" + re.escape(end),
        f"{start}\n{new_results}\n{end}",
        text,
        flags=re.DOTALL,
    )
    path.write_text(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate GEX / flow / VEX signals on captured data.")
    ap.add_argument("--symbol", default="SPY", help="Underlying symbol (default: SPY).")
    ap.add_argument("--module", default="gex", choices=MODULES,
                    help="Signal module to validate: gex, flow, or vex (default: gex).")
    ap.add_argument("--min-days", type=int, default=MIN_TRADING_DAYS,
                    help="Minimum trading days of snapshots required (default: 60).")
    ap.add_argument("--data-root", default=None,
                    help="Capture-store root (default: <repo>/data/chains).")
    ap.add_argument("--n-sessions-ahead", type=int, default=5,
                    help="Forward window (sessions) for flow/vex metrics (default: 5).")
    args = ap.parse_args(argv)

    symbol = args.symbol.upper()
    module = args.module
    days = loader.snapshot_days_covered(symbol, args.data_root)
    if days < args.min_days:
        extra = ""
        if module == "flow":
            extra = (
                " Flow baselines are cold-start until per-contract history "
                "accrues, and sweep confirmation still requires IBKR tick "
                "data."
            )
        elif module == "vex":
            extra = " True IV validation additionally needs an ATM-IV series."
        die(
            f"Insufficient data for {module} validation: {days} capture day(s) "
            f"for '{symbol}', need >= {args.min_days} trading days of forward "
            "snapshots before any result can be trusted." + extra + " No "
            "numbers were fabricated; the validation reports remain "
            "UNVALIDATED. Re-run `python3 backtests/harness/run_validation.py "
            f"--module {module} --symbol {symbol}` once forward capture has "
            "accumulated enough history. (Optional ThetaData historical "
            "purchase only with Jagadeesh's approval, per the data buy "
            "ladder.)",
            2,
        )

    if module == "gex":
        _check_gex_engine()
        snapshots = loader.load_snapshots(symbol, store_root=args.data_root)
        sessions = _build_sessions(symbol, snapshots)
        try:
            results = evaluate.run_all(sessions)
        except evaluate.InsufficientDataError as exc:
            die(f"Insufficient data for validation: {exc}", 2)
        for key, fname in REPORT_KEYS.items():
            _update_report(REPORTS_DIR / fname, key, results,
                           renderer=_render_results)
            print(f"updated {fname}")
        print(f"Validation complete on {len(sessions)} sessions for {symbol}.")
        return 0

    # flow / vex: build flow + VEX sessions, run the flow metrics.
    try:
        from turblance_trader.flow import compute_flow_profile  # noqa: F401
        from turblance_trader.vex import compute_vex_profile  # noqa: F401
    except Exception as exc:
        die(f"Flow/VEX engines not importable yet ({exc}).", 3)

    snapshots = loader.load_snapshots(symbol, store_root=args.data_root)
    sessions = _build_flow_vex_sessions(symbol, snapshots)
    try:
        results = evaluate.run_all_flow(
            sessions, n_sessions_ahead=args.n_sessions_ahead)
    except evaluate.InsufficientDataError as exc:
        die(f"Insufficient data for {module} validation: {exc}", 2)

    report_keys = FLOW_REPORT_KEYS if module == "flow" else VEX_REPORT_KEYS
    for key, fname in report_keys.items():
        _update_report(REPORTS_DIR / fname, key, results,
                       renderer=_render_flow_results)
        print(f"updated {fname}")
    print(f"{module} validation complete on {len(sessions)} sessions for {symbol}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
