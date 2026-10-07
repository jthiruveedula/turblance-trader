# terminal_data.json — schema for the turblance-trader front end

Real engine outputs, exported 2026-09-20 via scripts/export_terminal_data.py.
Every number comes from an actual engine run on a real banked CBOE delayed
chain snapshot. Nothing invented.

Top level:
- `metadata`: generated_at_utc, data_latency ("~15 min delayed (CBOE)"),
  experimental (true — signals unvalidated), data_budget_spent (0)
- `symbols`: SPY, SPX, QQQ, IWM

Per symbol (status "ok", or "no_data" if no snapshot banked):
- `spot`, `quote_time` (ISO-8601 UTC), `contracts` (chain size)
- `spot_history`: every banked snapshot's real observed print as
  [{t, spot}], sorted ascending. Draw as snapshot marks / a line series.
  NEVER as fabricated OHLC candles.
- `candles`: [] — intraday OHLC candles begin only with the IBKR live feed
  (pending Jagadeesh's gateway setup). Wire the candlestick series to this
  array plus a pushCandle() hook for the live phase; until it has data show
  an honest empty state, not invented candles.
- `gex`: zero_gamma_flip (float or null — null is the genuine engine result,
  no sign crossing found), call_wall, put_wall, king_node {strike, net_gex},
  gamma_regime ("positive"/"negative"), gex_by_strike [{strike, net_gex}]
  trimmed to ±10% of spot
- `flow`: unusual_strikes [] (empty = honest cold-start, no baselines yet),
  sweeps [] each with proxy:true, experimental:true
- `vex`: vega_flip (float or null), call_vega_wall, put_vega_wall,
  vex_king_node {strike, net_vex}, vol_regime

Front-end rules: label "~15 min delayed" and "experimental — signals
unvalidated" visibly. Never invent data beyond this file.
