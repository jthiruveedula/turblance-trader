# Validation Report — VEX Vol-Regime IV Read

**Signal:** `vex_regime` (backtests/harness/flow_signals.py), profile from
`src/turblance_trader/vex/`
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

The dealer vega (VEX) regime maps to a forward IV read: positive dealer vega
-> IV contraction (vol sellers dominate, IV decays); negative dealer vega ->
IV expansion (dealers buy vol on weakness); mixed -> no directional read.
Success metric: forecast accuracy — fraction of positive/negative-regime
days where forward realized volatility over the next N sessions (default
N=5) falls below (positive/contraction) or above (negative/expansion) the
scored-day median. Accuracy statistically above 50% (with a 95% Wilson CI
clearing it) would support the hypothesis; at-or-below would refute it.

## Methodology

1. For each trading day, build a VEX profile from the day's first
   forward-captured chain snapshot (`data/chains/{symbol}/{YYYY-MM-DD}/`).
   VEX is in dollars per 1 vol point (1%) IV move (dealer convention,
   short).
2. Take the profile's `vol_regime` ('positive'|'negative'|'mixed') and run
   `vex_regime` on it for the documented IV read.
3. Compute forward realized volatility as the population std of
   close-to-close returns over the next N sessions (closes from each day's
   snapshots' spot column) — an IV PROXY (see limitations).
4. A positive-regime day is "correct" when its forward realized vol is below
   the scored-session median; a negative-regime day is "correct" when above.
   Mixed-regime days are reported, not scored.
5. Report accuracy with a 95% Wilson score interval, split in-sample /
   out-of-sample (see below). Baseline: the 50% coin-flip rate.

## In-sample vs out-of-sample

- In-sample: first 60% of captured trading days (chronological).
- Out-of-sample: remaining 40% — the only numbers that count toward
  validation. No parameter may be re-tuned on this split.
- Minimum bar: 60 trading days total before the first validation run
  (see Re-validation trigger).

## Costs & slippage

No trade execution exists in the current harness, so costs change no number
in this report. When P&L attribution is added, the harness's explicit costs
hook applies (default: 2.0 bps slippage placeholder, $0 commission
placeholder) and this section will report net-of-cost economics.

## Sample size & confidence

- Sessions required: >= 60 trading days before the first run; the report is
  re-run as capture accumulates.
- All rates reported with 95% Wilson score intervals. Wide intervals on
  small samples are a finding, not a footnote.

## Honest limitations

- **The regime cutoffs are arbitrary heuristics** (total_net_vex /
  sum|net_vex| with +/-0.5 thresholds) and the mapping to IV direction is a
  mechanical dealer-hedging story, not an empirical finding. Nothing about
  this forecast has been measured.
- **Forward realized volatility is an IV proxy, not IV.** Realized vol and
  implied vol can diverge sharply (vol risk premium, event pricing). True
  validation needs an ATM-IV time series — planned via the IBKR path.
- The dealer-positioning convention ('short') is a flippable modeling
  assumption; the backtest layer can re-run under 'long'/'flat' for
  sensitivity analysis.
- A passing result would only justify building the IV series, not acting on
  the regime read.
- Single-symbol results do not generalize; per-symbol validation is required.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no numbers are reported here. Nothing
has been fabricated: every figure in this section will be computed by
`python3 backtests/harness/run_validation.py --module vex --symbol <SYM>`
on real forward-captured snapshots, in-sample vs out-of-sample, with costs
modeled. Until then, the VEX-regime IV read is an untested hypothesis built
on arbitrary cutoffs.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --module vex --symbol SPY`
after >= 60 trading days of forward snapshots have accumulated in
`data/chains/SPY/`; the runner refreshes the Results section above
automatically. Strengthen the test once an ATM-IV series is available via
the IBKR path (replacing the realized-vol proxy). (Optional ThetaData
historical purchase only with Jagadeesh's explicit approval, per the data
buy ladder — never spend without approval.)
