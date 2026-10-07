# Turblance Trader — Flow / VEX / Dark-Pool Spec

**Modules:** options-flow scanner · VEX (vega exposure) engine · FINRA ATS dark-pool analytics · **Status:** built, unit-tested · **Date:** 2026-09-20
**All code in these modules is original, written clean-room.** Public methodology knowledge (textbook option math, publicly described market-structure concepts, FINRA's published ATS file spec) informed the design; no third-party code, text, or proprietary implementation was copied.

---

## 1. What they do

Three detection modules plus one analytics module, all feeding the
backtest harness (`backtests/harness/flow_signals.py`, `evaluate.py`):

- **Flow (options-flow scanner):** turns a chain snapshot into a
  *flow profile* — per-strike unusual volume/OI scores plus sweep-like
  event proxies. Answers "where is unusual options activity, and does it
  look like coordinated multi-strike flow?"
- **VEX (vega exposure):** the dealer-positioning counterpart to GEX, in
  vega instead of gamma. Answers "where is dealer vol exposure, and what
  vol regime are we in?" All exposures in **dollars per 1 vol point (1%)
  IV move**.
- **Dark pool (FINRA ATS):** weekly off-exchange share/trade volume per
  symbol per ATS venue, ~2 weeks delayed by design. Historical structure
  research only — never a timing input.
- **Flow delta derivation** (`src/turblance_trader/capture/flow.py`):
  snapshot-to-snapshot flow deltas banked under `data/flow/deltas/`.

Pipeline: `capture` (chain snapshots) → `flow` / `vex` (profiles) →
`backtests/harness` (signal validation). ATS analytics run on the weekly
FINRA pull (`scripts/fetch_ats.py`), independent of the options pipeline.

## 2. Methodology and formulas

### 2.1 Unusual-activity z-scores (robust)

Per contract, session volume and open interest are scored against rolling
baselines (`compute_baselines`: per-contract median/MAD over the most
recent `window=20` samples, `min_samples=5`; thin histories degrade to a
per-bucket — symbol/expiry/option_type — baseline, then to "insufficient
baseline", never to a guess):

```
robust_z = (x − median) / (1.4826 × MAD)
```

- `NaN` when x or the baseline is missing; a flat history (MAD = 0)
  scores a capped ±6.0 with the deviation's sign (documented convention).
- `volume_z`: session volume vs its baseline. `oi_z`: open interest vs
  its baseline. `oi_change`: current OI minus previous-snapshot OI on the
  contract key (NaN without a previous snapshot).
- `contract_score = max(volume_z, oi_z)` clipped at 0; `NaN` in cold
  start. Per-strike `unusual_score` sums call+put contract scores;
  per-expiry `unusual_score` is the max of its strikes' scores (one loud
  strike lifts its expiry).
- Strikes with contract score ≥ `unusual_threshold` (2.0) and a usable
  baseline land in `.unusual_strikes`. **Cold start: with no baselines,
  scores are NaN, the list is empty, and nothing is fabricated.**

### 2.2 Sweep-proxy scoring

A sweep-like cluster = contracts on one snapshot sharing
(symbol, expiry, option_type, aggression side) where each leg:
(1) prints at/near the offer (`(last−bid)/(ask−bid) ≥ 0.75`, "lifted") or
bid (`≤ 0.25`, "hit") — midpoint prints are never legs; and
(2) has `volume_z ≥ 2.0` (or, in cold start, session volume ≥ 500 —
absolute-burst fallback, flagged via `used_burst_fallback`); with
(3) ≥ 3 distinct strikes.

Cluster score (heuristic strength, EXPERIMENTAL/UNVALIDATED — ranks
candidates, not a probability):

```
score = n_distinct_strikes × mean(clipped volume_z of legs)   # z clipped at 6
```

Multi-expiry rollup: clusters sharing (symbol, option_type, side) with
overlapping strike ranges merge into ONE event (anchor expiry = largest
volume contributor; all expiries preserved). **PROXY DISCLAIMER: these are
unconfirmed proxies.** Independent orders in one snapshot window can look
identical; confirmation needs tick-level trade data (IBKR path). Every
event carries `proxy=True`, `experimental=True`.

### 2.3 VEX units and formulas

Per-contract dealer vega exposure, in **dollars per 1 vol-point IV move**:

```
customer_vex = vega_per_1pt × open_interest × 100
dealer_vex   = dealer_sign × customer_vex        # short: −1 (default)
```

Vega source: the snapshot's `vega` column when finite (assumed quoted per
1.0 IV move — the textbook convention — divided by `VOL_POINT_DIVISOR =
100.0` to reach the per-1-point basis; `source_vega_iv_unit` declares the
source's quoting if it differs); otherwise Black-Scholes vega from
`implied_volatility` (same fallback path as GEX gamma). Rows missing both
are skipped and counted (`skipped_rows`).

Aggregation and levels mirror GEX: `by_strike` (call_vex, put_vex,
net_vex), `by_expiry`, **vega flip** (first strict sign change of net
dealer vega among meaningful-exposure strikes, interpolated), **call/put
vega walls** (max |call_vex| at/above spot; max |put_vex| at/below spot),
**VEX King** (max |net_vex|), **vol regime** =
`total_net_vex / Σ|net_vex|` with ±0.5 cutoffs → positive / negative /
mixed (**arbitrary heuristics — EXPERIMENTAL/UNVALIDATED**), and
`vex_velocity` for rate-of-change.

### 2.4 Public API

```python
from turblance_trader.flow import compute_flow_profile
flow = compute_flow_profile(df, prev_df=None, baselines=None)
flow.by_strike        # strike, call_volume, put_volume, volume_z, oi_change, sweep_score, unusual_score
flow.by_expiry        # expiry, total_volume, unusual_score, sweep_score
flow.spot             # float
flow.unusual_strikes  # [{strike, option_type, score, reason, experimental}]
flow.sweeps           # [{expiry, direction, strikes_hit, volume, score, ..., proxy, experimental}]
flow.cold_start       # True until baselines accrue
flow.to_dict()        # JSON-serializable summary

from turblance_trader.vex import compute_vex_profile
vex = compute_vex_profile(df, spot=None, risk_free_rate=0.0, dealer_position='short')
vex.by_strike         # strike, call_vex, put_vex, net_vex  (dollars per 1 vol point)
vex.by_expiry         # expiry, net_vex
vex.vega_flip         # float | None
vex.call_vega_wall / vex.put_vega_wall
vex.vex_king          # {'strike', 'net_vex'}
vex.vol_regime        # 'positive' | 'negative' | 'mixed'  (EXPERIMENTAL)
vex.to_dict() / vex.to_dataframe()
```

Backtest-layer signals (`backtests/harness/flow_signals.py`, all
EXPERIMENTAL): `unusual_volume_burst`, `sweep_followthrough`,
`vex_regime`, `darkpool_divergence`, plus `run_all()` / `new_context()`.

### 2.5 ATS analytics definitions

Pure functions over weekly (week_start, symbol) ATS rows
(`src/turblance_trader/darkpool/analytics.py`), NaN-safe, interpretive
outputs marked experimental:

- `weekly_symbol_totals`: per (week, symbol) ATS shares/trades, venue
  count, avg trade size.
- `ats_share_within_symbol`: each venue's share of its symbol's ATS volume.
- `concentration`: HHI + top-1/top-3 venue share (experimental).
- `week_over_week`: fractional + absolute WoW change per (symbol, venue).
- `dark_volume_share`: ATS shares / consolidated shares — needs a
  consolidated-volume input (the future TRF wiring point); **returns NaN
  with an honest note without it, never estimated** (experimental).
- `block_bucket_summary`: block-bucket mix when block rows exist
  (experimental).

## 3. Data sources

| Source | State | Data | Cost |
|---|---|---|---|
| CBOE delayed quotes | **Live now** (forward capture started 2026-09-20) | 15-min delayed chains, IV + Greeks + OI (volume column feeds the flow baselines) | $0 |
| FINRA ATS Transparency (weekly) | Adapter built, weekly cron; **delayed ~2 weeks** (Tier 1) by design | Per-security/per-ATS weekly share volume + trade counts | $0 |
| IBKR (TWS socket API + IB Gateway) | Adapter built & unit-tested; **needs Jagadeesh's setup** (see docs/gex-spec.md §3.2) | Real-time chains (finer volume/flow prints) + tick-level trade data for sweep confirmation; real-time TRF prints for consolidated dark volume | ~$11.50/mo after his setup |
| Schwab / Alpaca | Stubs (setup instructions only) | — | $0 with account |

**Budget:** Jagadeesh approved up to **$20/mo** for real-time GEX/VEX/flow
data. Standing rule: **$0-first** — do not subscribe to anything without
his explicit approval, and only propose spending if the free path proves
insufficient. No subscriptions exist in any of these modules' code paths;
no credentials anywhere; no real-money routing; no alert-delivery
mechanism (detection only).

### 3.1 Licensing / ToS boundaries

- CBOE ToS: automated downloading is prohibited — the CBOE adapter is the
  day-one bootstrap (polite polling, hourly cron, 429/403 = hard stop),
  not the durable source. See docs/gex-spec.md §3.1.
- FINRA ATS data is published for non-commercial personal use; this module
  downloads for Jagadeesh's own research, keeps it local (`data/darkpool/`,
  git-ignored), never republishes or displays it to third parties.
- OPRA redistribution (~$1,500/mo) does not apply: all data stays local,
  personal research use. If turblance-trader ever serves data to other
  users, the licensing picture changes — a pre-launch decision.

## 4. Assumptions

1. Dealers are net short all customer-long options (flippable
   `dealer_position`; VEX only — flow makes no dealer assumption).
2. Open interest proxies positioning; zero-OI strikes contribute zero VEX.
3. 100 shares per contract, uniformly.
4. Session volume is a usable flow proxy; `last`-vs-midpoint is a usable
   aggression proxy (both are approximations of trade direction).
5. Volume/OI baselines: the recent-20-sample rolling median is the
   "normal" level; deviations beyond robust z = 2 are "unusual".
6. Weekly ATS share volume is a usable dark-activity series; consolidated
   volume is unknown until the TRF path exists.
7. Risk-free rate defaults to 0; dividends ignored (small error on
   dividend-paying underlyings, flagged for refinement).

## 5. Limitations (honest)

- **Cold start:** no usable baselines exist until forward capture accrues
  per-contract history (~5+ snapshots per contract minimum). Early flow
  signals are NEUTRAL by design, not by evidence.
- **Sweep proxies are unconfirmed** until IBKR tick data; a passing
  backtest would only justify building the confirmation path.
- **VEX regime cutoffs (±0.5) are arbitrary heuristics**; the IV read is a
  mechanical story, not a measured effect; validation uses realized
  volatility as an IV *proxy* until an ATM-IV series exists.
- **ATS data is ~2 weeks delayed** — structure research only, never timing.
  It covers ATS prints only (not all off-exchange flow); `dark_volume_share`
  is NaN without consolidated volume, never estimated.
- CBOE data is 15-min delayed; hourly snapshots are coarse for
  velocity/flow analysis; IBKR real-time supersedes.
- Only 4 index symbols (SPY, SPX, QQQ, IWM) in the capture universe so far.
- The dealer's real positioning is unobserved; all exposure maps are
  assumption-flavored (`dealer_position` is flippable for sensitivity).

## 6. Validation status

- **Unit tests:** full suite green — flow engine (unusual-activity,
  sweep proxy, profile contract), VEX engine (exposure units, levels,
  velocity), dark-pool analytics, flow capture/deltas, plus the new
  flow/vex backtest tests (see §7). Stdlib only, no network in tests.
- **Signals:** `unusual_volume_burst`, `sweep_followthrough`,
  `vex_regime`, `darkpool_divergence` — all EXPERIMENTAL, all pure
  functions with honest rationales.
- **Metrics:** `unusual_burst_hit_rate`, `sweep_followthrough_rate`,
  `vex_regime_iv_accuracy` in `backtests/harness/evaluate.py` —
  `InsufficientDataError` on empty/insufficient input, in/out-of-sample
  splits, costs hook, 95% Wilson CIs. Nothing scores until ≥60 trading
  days of data.
- **Backtest reports** (`backtests/reports/flow-volume-burst.md`,
  `flow-sweep-followthrough.md`, `vex-regime.md`) follow the evidence
  standard; each concludes **UNVALIDATED — EXPERIMENTAL** with zero
  fabricated numbers and a concrete re-run trigger.
- **Forward capture:** hourly CBOE cron live (chain snapshots); weekly
  FINRA ATS pull cron; flow deltas banked under `data/flow/deltas/`.

## 7. How to run

```bash
cd ~/workspace/turblance-trader
python3 -m unittest discover -s tests            # full suite
python3 backtests/harness/run_validation.py --module flow --symbol SPY  # exits 2 until ≥60 days
python3 backtests/harness/run_validation.py --module vex --symbol SPY   # exits 2 until ≥60 days
python3 scripts/fetch_ats.py                     # weekly FINRA ATS pull (≥7-day cadence enforced)
```

## 8. What's next (recommended)

1. **Keep forward capture running** — flow baselines are the scarcest
   asset ("history you can't buy back"); ~5+ snapshots/contract before
   the first honest unusual read.
2. **Jagadeesh completes IBKR setup** (docs/gex-spec.md §3.2) →
   real-time chains, then the tick-confirmation path for sweep proxies,
   then the real-time TRF path for consolidated dark volume.
3. **Re-run validation** at ≥60 trading days:
   `--module flow` and `--module vex` refresh the three reports'
   Results sections automatically.
4. Replace the realized-vol IV proxy with an ATM-IV series; run the
   dealer-position sensitivity ('long'/'flat') on VEX metrics.
5. Optional: ThetaData/ORATS backfill only with Jagadeesh's explicit
   approval (per the data buy ladder — never spend without approval).
