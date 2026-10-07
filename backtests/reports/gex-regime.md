# Validation Report — Gamma-Regime Day-Type Classification

**Signal:** `gamma_regime` (backtests/harness/signals.py)
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

The sign of aggregate dealer gamma classifies the trading day:
positive gamma -> range day (fade the edges, avoid midpoints);
negative gamma -> trend day (momentum over fades);
mixed gamma -> whipsaw day (fade extremes or stand aside).
Success metric: forecast accuracy — a positive-gamma day is "correct" when
its (high-low)/open range falls below the all-session median range, and a
negative-gamma day is "correct" when its range falls above it. The regime
read passes if accuracy is statistically above 50% out-of-sample AND mean
positive-gamma range is below mean negative-gamma range (direction match).

## Methodology

1. For each trading day, take the regime read (`positive` / `negative` /
   `mixed`) from the day's GEX profile (last snapshot of the prior evening
   or first of the morning — frozen in-sample; no peeking at the day's
   price action).
2. Compute the day's range as (high - low) / first price from intraday
   spot prints.
3. Score per the rule above; mixed-regime days are reported but not scored.
4. Report accuracy with a 95% Wilson interval, plus mean range by regime
   and the direction-match check.
5. `backtests/harness/evaluate.py::regime_range_accuracy` implements exactly
   this.

## In-sample vs out-of-sample

- In-sample: first 60% of trading days — used to pick the regime thresholds
  (e.g. what counts as "mixed") and the range metric.
- Out-of-sample: remaining 40% — frozen definitions, the only numbers that
  count. Minimum 60 trading days total before the first validation run.

## Costs & slippage

No trade execution in the current harness, so costs change no number here.
The harness carries an explicit costs hook (default: 2.0 bps slippage
placeholder, $0 commission placeholder); when regime-conditioned strategy
P&L is attributed (fade edges on range days, momentum on trend days), this
section will report net-of-cost expectancy per regime.

## Sample size & confidence

- Days are the unit of observation; mixed days are unscored, shrinking the
  effective sample. Negative-gamma days are typically the minority regime —
  expect their statistics to be noisier.
- Accuracy reported with 95% Wilson score intervals. 50% accuracy with a
  wide interval is "no evidence", not "a weak signal".

## Honest limitations

- The median-split scoring rule is a coarse proxy: a positive-gamma day
  with a large range driven by scheduled news (FOMC/CPI) is not a regime
  failure, but this test counts it as one. Event-day handling is a v2
  refinement.
- Regime is a full-day label applied to intraday behavior; regimes can flip
  mid-session and this test ignores that.
- Range alone does not capture "whipsaw" (mixed days need a reversals-based
  metric — open for v2).
- The test validates the *classification*, not any particular way of
  trading it. A correct classification with negative net-of-cost expectancy
  is still not a tradeable signal.
- Per-symbol validation required; index behavior need not transfer to
  single names.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no accuracy figures are reported here.
Nothing has been fabricated: the accuracy table in this section will be
populated by `python3 backtests/harness/run_validation.py --symbol <SYM>`
on real forward-captured snapshots, in-sample vs out-of-sample, with costs
modeled. Until then, the gamma-regime day-type map is an untested
hypothesis.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --symbol SPY` after
>= 60 trading days of forward snapshots have accumulated in
`data/chains/SPY/`; the runner refreshes the Results section above
automatically. (Optional ThetaData historical purchase only with Jagadeesh's
explicit approval, per the data buy ladder — never spend without approval.)
