#!/usr/bin/env python3
"""Capture one options-chain snapshot per symbol and exit.

Cron-friendly and idempotent:
  * Outside US market hours it exits quietly (code 0, no snapshot) unless
    ``--force`` is given.
  * A snapshot whose quote_time was already captured is skipped, not
    duplicated (use ``--force`` to overwrite).

Exit codes:
  0  success, or cleanly skipped outside market hours
  2  usage error (bad arguments / unknown adapter)
  3  source blocked us (HTTP 429/403) — HARD STOP, investigate before rerunning
  4  fetch/parse/schema failure for at least one symbol
  5  snapshot write failure

Examples:
  python3 scripts/capture_chains.py --once
  python3 scripts/capture_chains.py --once --symbols SPY,SPX --force
  python3 scripts/capture_chains.py --once --source ibkr   # via local IB Gateway
  python3 scripts/capture_chains.py --once --source ibkr --capture-ticks
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Repo root = parent of this script's directory, so the runner works from cron
# without installation.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.capture.adapters import (  # noqa: E402
    ADAPTERS,
    ChainSourceError,
    RateLimitedError,
    SetupRequiredError,
)
from turblance_trader.capture.flow import derive_day_deltas, write_tick_prints  # noqa: E402
from turblance_trader.capture.market_hours import is_market_open  # noqa: E402
from turblance_trader.capture.store import write_snapshot  # noqa: E402

DEFAULT_SYMBOLS = ["SPY", "SPX", "QQQ", "IWM"]
PAUSE_BETWEEN_SYMBOLS = 3.0  # seconds; politeness, single-threaded


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--once",
        action="store_true",
        help="take one snapshot per symbol and exit (the only mode)",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="comma-separated underlyings (default: %(default)s)",
    )
    parser.add_argument(
        "--source",
        "--adapter",
        dest="adapter",
        default="cboe",
        choices=sorted(ADAPTERS),
        help="quote source adapter: cboe (free delayed), ibkr (gateway), "
        "schwab/alpaca (stubs) (default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "data"),
        help="snapshot root directory (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="capture even outside market hours; overwrite existing snapshots",
    )
    parser.add_argument(
        "--derive-flow-deltas",
        dest="derive_flow_deltas",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="after each snapshot, derive per-contract volume/OI flow deltas "
        "from the banked snapshots (pure local computation, zero network "
        "I/O). Use --no-derive-flow-deltas to skip. (default: on)",
    )
    parser.add_argument(
        "--capture-ticks",
        action="store_true",
        help="capture IBKR tick-by-tick trade prints into data/flow/ticks/ "
        "(requires --source ibkr; no-ops cleanly with no gateway)",
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.print_usage(sys.stderr)
        sys.exit(2)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        log("ERROR: no symbols given")
        return 2

    adapter_cls = ADAPTERS[args.adapter]
    log(f"adapter={args.adapter} symbols={','.join(symbols)} force={args.force}")

    if args.capture_ticks and args.adapter != "ibkr":
        log("ERROR: --capture-ticks requires --source ibkr")
        return 2

    if not args.force and not is_market_open():
        log("market closed — nothing to capture (use --force to override)")
        return 0

    try:
        adapter = adapter_cls()
    except SetupRequiredError as exc:
        log(f"ERROR: {exc}")
        return 2

    failures = 0
    for i, symbol in enumerate(symbols):
        if i:
            time.sleep(PAUSE_BETWEEN_SYMBOLS)
        try:
            df = adapter.capture(symbol)
        except RateLimitedError as exc:
            log(f"BLOCKED: {exc}")
            return 3  # hard stop — do not continue with other symbols
        except SetupRequiredError as exc:
            log(f"ERROR: {exc}")
            return 2
        except ChainSourceError as exc:
            log(f"ERROR capturing {symbol}: {exc}")
            failures += 1
            continue
        try:
            path, written = write_snapshot(df, args.data_dir, force=args.force)
        except Exception as exc:  # noqa: BLE001 - report and keep exit code
            log(f"ERROR writing snapshot for {symbol}: {exc}")
            return 5
        quote_time = df["quote_time"].iloc[0]
        action = "wrote" if written else "skipped (already captured)"
        log(f"{symbol}: {action} {len(df)} contracts, quote_time={quote_time} -> {path}")

        # Flow layer: derive per-contract volume/OI deltas from the banked
        # snapshots. Pure local computation — no network, no extra CBOE
        # requests. Never allowed to fail the chain capture itself.
        if args.derive_flow_deltas:
            try:
                date = str(quote_time)[:10]  # YYYY-MM-DD of the quote (UTC)
                for dpath, dwritten in derive_day_deltas(
                    symbol, date, args.data_dir
                ):
                    if dwritten:
                        log(f"{symbol}: flow deltas -> {dpath}")
            except Exception as exc:  # noqa: BLE001 - deltas are best effort
                log(f"WARNING: flow-delta derivation failed for {symbol}: {exc}")

        # IBKR tick prints (opt-in). No-ops cleanly without a live gateway.
        if args.capture_ticks:
            try:
                ticks = adapter.fetch_ticks(symbol)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - ticks are best effort
                log(f"WARNING: tick capture failed for {symbol}: {exc}")
                ticks = None
            if ticks is None:
                log(f"{symbol}: no tick prints (gateway unavailable?) — skipped")
            else:
                tpath, twritten = write_tick_prints(ticks, args.data_dir)
                taction = "wrote" if twritten else "skipped (already captured)"
                log(f"{symbol}: {taction} {len(ticks)} prints -> {tpath}")

    if failures:
        log(f"done with {failures} symbol failure(s)")
        return 4
    log("done — all symbols captured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
