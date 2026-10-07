# Validation Report — Node Tap-Decay Reaction Probabilities

**Signal:** `node_tap_decay` (backtests/harness/signals.py)
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

When price touches a major dealer node (King node, call wall, put wall),
the probability that the node "holds" (price reacts away from it rather than
breaking through) decays with each repeated touch of that node within the
session. The published claim under test is a specific schedule: ~80% on the
1st touch, ~66% on the 2nd, ~33% on the 3rd, ~10% on the 4th and beyond.
Success metric: observed reaction rate per touch-number bucket (1, 2, 3, 4+)
with 95% Wilson intervals, compared bucket-by-bucket against the claimed
schedule. The claim passes if observed rates track the schedule within
confidence bounds out-of-sample; it fails if any bucket's interval excludes
the claimed value.

## Methodology

1. For each trading day, take node levels (King, call wall, put wall) from
   the day's first GEX profile — no peeking.
2. Walk intraday prices in time order; record a touch when price comes
   within 0.05% of a node after having been away from it.
3. For each touch, look at the window until the next touch or 30 minutes,
   whichever is first. A **reaction** = price moved away from the node by
   >= 0.10% without crossing through it by that amount; crossing through =
   breakthrough = no reaction. Touches with no price prints in their window
   are "undetermined" and excluded from buckets (counted separately).
4. Bucket by touch number of that node within the session: 1st, 2nd, 3rd,
   4th+. Compare observed rates to 0.80 / 0.66 / 0.33 / 0.10.
5. `backtests/harness/evaluate.py::tap_decay_observed` implements exactly
   this; definitions are versioned there.

## In-sample vs out-of-sample

- In-sample: first 60% of trading days — used to pick the touch tolerance,
  breakthrough threshold, and reaction window.
- Out-of-sample: remaining 40% — frozen parameters, the only numbers that
  count. Minimum 60 trading days total before the first validation run.

## Costs & slippage

No trade execution in the current harness, so costs change no number here.
The harness carries an explicit costs hook (default: 2.0 bps slippage
placeholder, $0 commission placeholder); when fade-the-touch P&L is
attributed, this section will report per-bucket expectancy net of costs —
the number that actually decides whether the signal is tradeable.

## Sample size & confidence

- Touches are the unit of observation, not days: bucket 4+ will be sparse
  and its interval will be wide. That is expected and must be shown, not
  hidden.
- All rates with 95% Wilson score intervals. A bucket with fewer than ~30
  touches cannot confirm or refute its claimed rate and will be labeled
  inconclusive.

## Honest limitations

- "Reaction" is a definitional choice (away-move >= 0.10% without
  breakthrough). Different thresholds give different rates; the definition
  must be frozen in-sample and disclosed, never tuned to fit.
- Touch detection depends on snapshot/price cadence: sparse data misses
  touches; dense data over-counts grazes. Cadence must be reported with
  results.
- Node levels are taken from the first profile of the day; intraday node
  migration is ignored in this version — a known simplification.
- The 80/66/33/10 schedule is the *claim being tested*, not a prior to be
  assumed. If the data says otherwise, the data wins.
- Per-symbol, per-regime effects are not separated in v1.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no observed rates are reported here.
Nothing has been fabricated: the touch-bucket table in this section will be
populated by `python3 backtests/harness/run_validation.py --symbol <SYM>`
on real forward-captured snapshots, in-sample vs out-of-sample, with costs
modeled. Until then, the 80/66/33/10 tap-decay schedule is an untested
claim.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --symbol SPY` after
>= 60 trading days of forward snapshots have accumulated in
`data/chains/SPY/`; the runner refreshes the Results section above
automatically. (Optional ThetaData historical purchase only with Jagadeesh's
explicit approval, per the data buy ladder — never spend without approval.)
