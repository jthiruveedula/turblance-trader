"""Backtest harness for turblance-trader GEX signals.

Submodules
----------
loader    Load option-chain snapshot files from the forward-capture store
          into time-ordered (quote_time, DataFrame) sequences.
signals   Candidate GEX signals (king_magnet, node_tap_decay, gamma_regime).
          Every signal is EXPERIMENTAL — labeled as such in its docstring —
          until validated on real data.
evaluate  Score signal outcomes on dated profile sequences + price series,
          with an explicit costs/slippage hook.
run_validation
          CLI entry point that runs the full validation when data exists and
          refreshes the validation reports in backtests/reports/.

Status: EXPERIMENTAL / UNVALIDATED. Forward capture only just started, so
there is no historical options data to validate against yet.
"""

from backtests.harness import loader, signals, evaluate  # noqa: F401
