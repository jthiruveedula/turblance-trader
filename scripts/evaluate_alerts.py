#!/usr/bin/env python3
"""Evaluate turblance-trader signals and fire change alerts.

Cron-friendly:
  * Self-gates on US market hours (Mon-Fri 09:30-16:00 ET = 08:30-15:00 CT,
    NYSE holidays) unless --force is given; outside hours it prints
    "market closed — no evaluation" and exits 0.
  * Prints "ALERT: ..." lines when alerts fire, else "no alert — quiet".
  * Exit 0 always (a quiet market is not a failure).
  * Banks one ATM-IV observation and one suggestion per symbol per day
    (drives the honest IV-rank and confidence gates) — no duplicates.

Honesty note: evaluations run every 15 minutes, but the engine can only
react to NEW snapshots. With the current hourly CBOE delayed feed, effective
sensitivity is hourly until the IBKR live path exists. The CBOE pull rate is
NOT increased by this script.

Exit codes:
  0  evaluated (or cleanly skipped outside market hours)
  2  usage error
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Repo root = parent of this script's directory, so the runner works from cron
# without installation.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.alerts import default_state_path, evaluate_all  # noqa: E402
from turblance_trader.capture.market_hours import is_market_open  # noqa: E402
from turblance_trader.signals import record_atm_iv  # noqa: E402
from turblance_trader.signals.suggestions import record_suggestion  # noqa: E402

CT = ZoneInfo("America/Chicago")
DEFAULT_SYMBOLS = ["SPY", "SPX", "QQQ", "IWM"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    p.add_argument("--state-path", default=None,
                   help="alert state file (default: <data-dir>/alerts/state.json)")
    p.add_argument("--force", action="store_true",
                   help="bypass the market-hours gate (testing/backfill)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    data_dir = Path(args.data_dir)
    state_path = Path(args.state_path) if args.state_path else default_state_path(data_dir)
    today = datetime.now(CT).date().isoformat()

    if not args.force and not is_market_open():
        print("market closed — no evaluation")
        return 0

    history_path = data_dir / "signals" / "atm_iv.json"
    signal_history = data_dir / "signals" / "signal_history.json"

    alerts, reads = evaluate_all(
        data_dir, symbols, state_path, today, history_path=signal_history)

    # Bank one ATM-IV observation + one suggestion per symbol per day.
    for symbol, read in reads.items():
        record_atm_iv(symbol, read["iv"].get("atm_iv"), history_path, day=today)
        record_suggestion(symbol, read["suggestion"],
                          read["instinct"]["score"], signal_history, day=today)

    if alerts:
        for a in alerts:
            print(f"ALERT: {a['text']}")
    else:
        print("no alert — quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
