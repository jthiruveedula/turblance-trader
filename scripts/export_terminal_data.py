#!/usr/bin/env python3
"""Export the front-end terminal data payload from real banked snapshots.

For each of SPY/SPX/QQQ/IWM: load the LATEST banked chain snapshot, run the
real GEX, flow, and VEX engines on it, and write one JSON payload for the
front-end artifact. No invented data: every number comes from an engine run
on a real banked snapshot. A symbol with no snapshot is exported with
``"status": "no_data"``.

Usage:
    python3 scripts/export_terminal_data.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))  # package lives in src/, not pip-installed

import pandas as pd

from turblance_trader.flow.profile import compute_flow_profile
from turblance_trader.gex import compute_profile
from turblance_trader.vex import compute_vex_profile

SYMBOLS = ["SPY", "SPX", "QQQ", "IWM"]
DATA_DIR = REPO_ROOT / "data" / "chains"
OUT_PATH = REPO_ROOT / "terminal_data" / "terminal_data.json"


def _f(x):
    """JSON-safe float: NaN/None/junk -> None, never raises."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def latest_snapshot(symbol):
    """Path to the latest banked snapshot CSV for a symbol, or None."""
    sym_dir = DATA_DIR / symbol
    if not sym_dir.is_dir():
        return None
    date_dirs = sorted(d for d in sym_dir.iterdir() if d.is_dir())
    if not date_dirs:
        return None
    files = sorted(date_dirs[-1].glob("snapshot_*.csv"))
    return files[-1] if files else None


def spot_history(symbol):
    """All banked (quote_time, spot) points for a symbol, sorted, deduped.

    One point per banked snapshot (first data row's spot). These are real
    observed prints; the front end draws them as snapshot marks, never as
    fabricated OHLC candles.
    """
    sym_dir = DATA_DIR / symbol
    seen = {}
    if sym_dir.is_dir():
        for f in sorted(sym_dir.rglob("snapshot_*.csv")):
            try:
                row = pd.read_csv(f, usecols=["quote_time", "spot"], nrows=1)
            except (ValueError, pd.errors.EmptyDataError):
                continue
            if len(row) == 0:
                continue
            t, s = str(row["quote_time"].iloc[0]), _f(row["spot"].iloc[0])
            if t and s is not None and t not in seen:
                seen[t] = s
    return [{"t": t, "spot": s} for t, s in sorted(seen.items())]


def export_symbol(symbol):
    """Run all three engines on the symbol's latest snapshot."""
    path = latest_snapshot(symbol)
    if path is None:
        return {"symbol": symbol, "status": "no_data"}

    df = pd.read_csv(path)

    # Real engine runs — cold start for flow (no baselines, no prior
    # snapshot); baselines do not exist yet in forward capture.
    gex = compute_profile(df)
    flow = compute_flow_profile(df)
    vex = compute_vex_profile(df)

    spot = _f(gex.spot)

    # Per-strike net dollar gamma, compact: strikes within +/-10% of spot.
    gex_strikes = []
    if spot is not None and spot > 0:
        lo, hi = spot * 0.9, spot * 1.1
        band = gex.by_strike[
            (gex.by_strike["strike"] >= lo) & (gex.by_strike["strike"] <= hi)
        ]
        for rec in band.to_dict("records"):
            gex_strikes.append({
                "strike": _f(rec["strike"]),
                "call_gex": _f(rec["call_gex"]),
                "put_gex": _f(rec["put_gex"]),
                "net_gex": _f(rec["net_gex"]),
            })

    unusual = [
        {
            "strike": _f(u["strike"]),
            "direction": u["option_type"],
            "z_score": _f(u["score"]),
            "reason": u["reason"],
            "experimental": True,
        }
        for u in flow.unusual_strikes
    ]

    sweeps = []
    for s in flow.to_dict()["sweeps"]:
        sweeps.append({
            "expiry": s["expiry"],
            "direction": s["direction"],
            "side": s.get("side"),
            "n_strikes": s.get("n_strikes"),
            "strikes_hit": s["strikes_hit"],
            "expiries": s["expiries"],
            "volume": s["volume"],
            "score": s["score"],
            "used_burst_fallback": s["used_burst_fallback"],
            "proxy": True,
            "experimental": True,
        })

    return {
        "symbol": symbol,
        "status": "ok",
        "quote_time": str(df["quote_time"].iloc[0]) if len(df) else None,
        "snapshot_file": path.name,
        "n_contracts": int(len(df)),
        "spot": spot,
        # Real observed prints (one per banked snapshot). Front end draws
        # these as snapshot marks. "candles" stays empty until the IBKR live
        # feed provides true intraday OHLC — never synthesize candles here.
        "spot_history": spot_history(symbol),
        "candles": [],
        "candles_note": (
            "Intraday OHLC candles begin with the IBKR live feed (pending "
            "Jagadeesh's gateway setup). Until then the chart shows real "
            "snapshot marks only."
        ),
        "gex": {
            "zero_gamma_flip": _f(gex.zero_gamma_flip),
            "call_wall": _f(gex.call_wall),
            "put_wall": _f(gex.put_wall),
            "king_node": {
                "strike": _f(gex.king_node.get("strike")),
                "net_gex": _f(gex.king_node.get("net_gex")),
            },
            "gamma_regime": gex.regime,
            "total_net_gex": _f(gex.by_strike["net_gex"].sum()),
            "n_strikes": int(len(gex.by_strike)),
            "skipped_rows": int(gex.skipped_rows),
            "by_strike": gex_strikes,
            "units": "$ per 1% move (dealer convention, dealers assumed short)",
            "experimental": True,
        },
        "vex": {
            "vega_flip": _f(vex.vega_flip),
            "call_vega_wall": _f(vex.call_vega_wall),
            "put_vega_wall": _f(vex.put_vega_wall),
            "vex_king_node": {
                "strike": _f(vex.vex_king.get("strike")),
                "net_vex": _f(vex.vex_king.get("net_vex")),
            },
            "vol_regime": vex.vol_regime,
            "total_net_vex": _f(vex.by_strike["net_vex"].sum()),
            "n_strikes": int(len(vex.by_strike)),
            "skipped_rows": int(vex.skipped_rows),
            "units": "$ per 1 vol-point (1%) IV move (dealer convention, dealers assumed short)",
            "experimental": True,
        },
        "flow": {
            "cold_start": bool(flow.cold_start),
            "unusual_threshold": float(flow.unusual_threshold),
            "unusual_strikes": unusual,
            "n_unusual_strikes": len(unusual),
            "sweeps": sweeps,
            "n_sweeps": len(sweeps),
            "experimental": True,
        },
    }


def main():
    payload = {
        "metadata": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_latency": "~15 min delayed (CBOE)",
            "experimental": True,
            "experimental_note": (
                "Signals are UNVALIDATED until backtested on >=60 trading days "
                "of forward-captured data. Level measurements are mechanical "
                "derivations of the input chain; interpretations are experimental."
            ),
            "data_budget_spent": 0,
            "dealer_positioning_assumption": "dealers_short (flippable in the engines)",
            "source": "banked CBOE delayed-quotes snapshots",
        },
        "symbols": {sym: export_symbol(sym) for sym in SYMBOLS},
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
