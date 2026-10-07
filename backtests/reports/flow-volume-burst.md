# Validation Report — Unusual-Volume Burst

**Signal:** `unusual_volume_burst` (backtests/harness/flow_signals.py)
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

When the top unusual strikes (by composite volume/OI score) cluster on one
side (calls) and sit directionally relative to spot (calls above spot =
upside flow, puts below spot = downside flow), the session's remaining price
action continues in that direction — institutional-sized flow front-runs the
move. Success metric: the hit rate — fraction of directional bursts where
the sign of the forward N-session close move (default N=5) matches the
signal's bias. A hit rate statistically above 50% (with a 95% Wilson CI
clearing it) would support the hypothesis; at-or-below would refute it.

## Methodology

1. For each trading day, build a flow profile from the day's first
   forward-captured chain snapshot (`data/chains/{symbol}/{YYYY-MM-DD}/`),
   with baselines computed progressively from earlier days only (no
   peeking: day N's profile never sees day N or later).
2. Run `unusual_volume_burst` on the profile: top-5 unusual strikes by
   score; directional only when the dominant side clusters directionally
   relative to spot.
3. Score the signal against the forward N-session close move (closes taken
   from each day's snapshots' spot column; no external price feed).
4. Report hit rate with a 95% Wilson score interval, split in-sample /
   out-of-sample (see below).
5. Baseline comparison: the 50% coin-flip rate is the minimum bar. Neutral
   days (no directional burst), days without a flow profile, and days
   without a full forward window are reported but never enter the
   denominator.

## In-sample vs out-of-sample

- In-sample: first 60% of captured trading days (chronological).
- Out-of-sample: remaining 40% — the only numbers that count toward
  validation. No parameter (top_n, thresholds) may be re-tuned on this
  split.
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

- **Cold start:** baselines need ~5+ snapshots of per-contract history
  (`compute_baselines` min_samples). The first days of capture produce
  NEUTRAL signals by design — early "no signal" days are honest, not
  missing data.
- The unusual-score threshold (2.0) and top_n (5) are arbitrary until tuned
  in-sample — they must not be tuned out-of-sample.
- Unusual volume can be expiry-driven (0DTE hedging, rebalancing) rather
  than directional information; the test cannot distinguish the two.
- A hit is defined on the forward window, not on tradable entries/exits —
  this test says nothing about P&L.
- Single-symbol results do not generalize; per-symbol validation is required.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no numbers are reported here. Nothing
has been fabricated: every figure in this section will be computed by
`python3 backtests/harness/run_validation.py --module flow --symbol <SYM>`
on real forward-captured snapshots, in-sample vs out-of-sample, with costs
modeled. Until then, the unusual-volume-burst effect is an untested
hypothesis.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --module flow --symbol SPY`
after >= 60 trading days of forward flow snapshots have accumulated in
`data/chains/SPY/` AND IBKR tick data is available for sweep confirmation
(see flow-sweep-followthrough.md); the runner refreshes the Results section
above automatically. (Optional ThetaData historical purchase only with
Jagadeesh's explicit approval, per the data buy ladder — never spend
without approval.)
