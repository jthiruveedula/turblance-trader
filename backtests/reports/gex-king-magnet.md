# Validation Report — King-Node Magnet Effect

**Signal:** `king_magnet` (backtests/harness/signals.py)
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

Per session, the underlying's price is drawn toward the King node — the
strike with the largest absolute dealer gamma exposure — ahead of the close,
because mechanical dealer hedging flows pull price toward the point of
maximum exposure. Success metric: the fraction of sessions in which any
intraday price print comes within 0.05% of the King-node strike before the
close (the "hit rate"). A hit rate statistically above a no-structure
baseline (e.g. random-walk simulation matched on daily range) would support
the hypothesis; at-or-below baseline would refute it.

## Methodology

1. For each trading day, build GEX profiles from forward-captured chain
   snapshots (`data/chains/{symbol}/{YYYY-MM-DD}/`).
2. Take the King-node strike from the first profile of the day (no peeking
   at later snapshots when forming the day's forecast).
3. Walk the day's intraday price series (spot column of the snapshots);
   record a hit if any print lands within 0.05% of the King strike.
4. Report hit rate with a 95% Wilson score interval, split in-sample /
   out-of-sample (see below).
5. Baseline comparison: Monte-Carlo random walks with the day's realized
   volatility, measuring how often they "touch" a fixed level at the same
   distance from the open — the magnet claim must beat this.

## In-sample vs out-of-sample

- In-sample: first 60% of captured trading days — used to pick the touch
  tolerance and any distance filters.
- Out-of-sample: remaining 40% — the only numbers that count toward
  validation. No parameter may be re-tuned on this split.
- Minimum bar: 60 trading days total before the first validation run
  (see Re-validation trigger).

## Costs & slippage

No trade execution exists in the current harness, so costs change no number
in this report. When P&L attribution is added, the harness's explicit costs
hook applies (default: 2.0 bps slippage placeholder, $0 commission
placeholder) and this section will report net-of-cost hit economics.

## Sample size & confidence

- Sessions required: >= 60 trading days before the first run; the report is
  re-run as capture accumulates.
- All rates reported with 95% Wilson score intervals. Wide intervals on
  small samples are a finding, not a footnote.

## Honest limitations

- The King node is identified from the *first* snapshot of the day; nodes
  migrate intraday, and a stale node invalidates the day's forecast.
- "Touch" tolerance (0.05%) is arbitrary until tuned in-sample — it must not
  be tuned out-of-sample.
- Correlation is not mechanism: a high hit rate could reflect pinning by
  other flows (e.g. options-expiry pinning) rather than dealer hedging.
- Single-symbol results do not generalize; per-symbol validation is required.
- This test says nothing about *tradability* — touching a level is not a P&L.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no numbers are reported here. Nothing
has been fabricated: every figure in this section will be computed by
`python3 backtests/harness/run_validation.py --symbol <SYM>` on real
forward-captured snapshots, in-sample vs out-of-sample, with costs modeled.
Until then, the King-node magnet effect is an untested hypothesis.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --symbol SPY` after
>= 60 trading days of forward snapshots have accumulated in
`data/chains/SPY/`; the runner refreshes the Results section above
automatically. (Optional ThetaData historical purchase only with Jagadeesh's
explicit approval, per the data buy ladder — never spend without approval.)
