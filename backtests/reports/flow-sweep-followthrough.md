# Validation Report — Sweep Follow-Through

**Signal:** `sweep_followthrough` (backtests/harness/flow_signals.py)
**Status: UNVALIDATED — EXPERIMENTAL.** Do not trade on this. Do not present it as reliable.
**Date:** 2026-09-20

## Hypothesis

A sweep-like event — aggressive prints (at/near bid or offer) plus elevated
volume across >= 3 strikes of one direction/side — leaves directional
pressure over the next N sessions (default N=5): lifted calls / hit puts ->
bullish pressure; hit calls / lifted puts -> bearish pressure. Success
metric: the follow-through rate — fraction of directional events where the
sign of the forward N-session close move matches the hypothesized pressure.
A rate statistically above 50% (with a 95% Wilson CI clearing it) would
support the hypothesis; at-or-below would refute it.

## Methodology

1. For each trading day, build a flow profile from the day's first
   forward-captured chain snapshot (`data/chains/{symbol}/{YYYY-MM-DD}/`),
   with baselines computed progressively from earlier days only.
2. Run `sweep_followthrough` on the profile: the top-scoring sweep-like
   event maps to a hypothesized bias via (direction, side). Days with no
   events are neutral.
3. Score each directional event against the forward N-session close move
   (closes from each day's snapshots' spot column).
4. Report the follow-through rate with a 95% Wilson score interval, split
   in-sample / out-of-sample (see below).
5. Baseline: the 50% coin-flip rate. Neutral days and days without a full
   forward window are reported, never scored.

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

- **These are UNCONFIRMED PROXIES, not confirmed sweeps.** A chain snapshot
  shows aggressive prints + elevated volume across strikes; independent
  orders arriving in the same snapshot window can look identical. Every
  scored event carries `proxy=True` at the engine layer. Confirming a sweep
  requires tick-level trade data (time, price, size, exchange per print) —
  planned via the IBKR path, not yet available.
- **Even a "passing" result here would NOT validate trading the proxies.**
  It would only justify building the IBKR tick-confirmation path so that
  real sweeps can be tested.
- Sweep-proxy knobs (min_strikes=3, volume_z_min=2.0, lift/hit thresholds,
  burst fallback) are heuristics; multi-expiry merging can fuse independent
  same-direction bursts into one event (documented in the engine).
- A forward-window hit is not a tradable P&L — this test says nothing about
  execution.

## Results

<!-- RESULTS:START -->
**UNVALIDATED — EXPERIMENTAL.** Forward capture only just started; there is
no historical options data yet, and no sweep event has ever been confirmed
against tick data. No numbers are reported here. Nothing has been
fabricated: every figure in this section will be computed by
`python3 backtests/harness/run_validation.py --module flow --symbol <SYM>`
on real forward-captured snapshots, in-sample vs out-of-sample, with costs
modeled. Until then — and until IBKR tick data confirms the events — sweep
follow-through is an untested hypothesis about unconfirmed proxies.
<!-- RESULTS:END -->

## Re-validation trigger

Re-run `python3 backtests/harness/run_validation.py --module flow --symbol SPY`
after >= 60 trading days of forward flow snapshots have accumulated in
`data/chains/SPY/` **and** IBKR tick data is available so that flagged
events can be confirmed (or rejected) against tick-level prints; the runner
refreshes the Results section above automatically. Sweep validation is
provisional until that confirmation path exists. (Optional ThetaData
historical purchase only with Jagadeesh's explicit approval, per the data
buy ladder — never spend without approval.)
