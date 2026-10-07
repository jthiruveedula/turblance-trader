# Turblance Trader — Signals Spec

**Module:** signal engine (`src/turblance_trader/signals/`) · **Status:** built, unit-tested · **Date:** 2026-09-20
**All code in this module is original, written clean-room.** Public methodology knowledge (textbook option math, publicly described market-structure concepts) informed the design; no third-party code, text, or proprietary implementation was copied.

---

## 1. What it does

Turns engine outputs (GEX profile, flow profile, VEX profile) plus IV
factors from a single chain snapshot into:

1. An **instinct score** (−100…+100) with a band label and full component
   breakdown (transparency: the math is shown, never a black box).
2. **IV factors**: ATM IV, put-call skew, term-structure slope — mechanical
   measurements of the snapshot's vol surface.
3. A **suggestion**: one of six actions, each with a one-line rationale
   naming its drivers, a confidence label, the experimental marker, and
   the "personal research tool, not financial advice" disclaimer.

Pipeline: `capture` (snapshots) → `gex` / `flow` / `vex` (profiles) →
`signals` (instinct + IV + suggestion) → `alerts` (change detection) →
`scripts/evaluate_alerts.py` (15-min evaluations) and
`scripts/postmarket_summary.py` (daily recap).

## 2. Instinct score — formula

Inputs: spot `S`, zero-gamma flip `F` (may be None), call wall `CW`,
put wall `PW`, King node `K`, gamma regime `R` (±1 or string),
sweeps = list of `(d, b)` with `d ∈ {+1 bullish, −1 bearish}` and
`b` = breadth in distinct strikes, vol regime `V` (±1 or string).

```
r1 = +1 if (F and S > F) or (F is None and R > 0) else −1
r2 = +1 if |S − PW|/S ≤ 0.005 else −1 if |S − CW|/S ≤ 0.005 else 0
pinned = |S − K|/S ≤ 0.003            → conviction halved (pin = chop)
r3 = sign(Σ d·min(b, 3)) capped to ±1, 0 when no sweeps
r4 = 0.5 · V
score = clamp(round(100 · (r1 + r2 + r3 + r4) / 3.5), −100, 100)
```

Bands: ≥40 **BULLISH**, 15–39 **LEAN BULLISH**, −14–14 **NEUTRAL**,
−39–−15 **LEAN BEARISH**, ≤−40 **BEARISH**.

Design notes (all documented in code, all unit-tested):
- Cutoffs (0.5% walls, 0.3% pin, ±40 bands) are fixed heuristics —
  EXPERIMENTAL, not measured effects.
- Sweep breadth is capped at 3 per sweep so one giant proxy cluster
  cannot dominate the score.
- Resistance takes precedence over support if spot is within 0.5% of
  both walls (narrow-range day).
- `sweep_direction()`: calls-lifted or puts-hit → bullish; puts-lifted or
  calls-hit → bearish; mid/unknown prints → None (never guessed).

## 3. IV factors — definitions

From a **single snapshot** (no history needed):

| Factor | Definition |
|---|---|
| ATM IV | IV of the contract nearest spot on the nearest expiry |
| Put-call skew | mean put IV − mean call IV, front expiry, strikes within ±5% of spot; None with <3 legs per side. Positive = downside fear priced |
| Term-structure slope | second-expiry ATM IV − front-expiry ATM IV; None with <2 expiries. Positive = upward-sloping ("patience is cheap") |

**IV rank** needs history and is honestly gated: `iv_rank()` returns
`{"status": "insufficient_history"}` until **20+ trading days** of ATM-IV
observations exist for the symbol. History is banked by the alert
evaluator (`data/signals/atm_iv.json`), one observation per symbol per
day — implausible values (<2% ATM, the pre-2026-09-20-fix unit bug) are
refused, and `iv_factors()` returns an honest `iv_note` instead of a
wrong number when a snapshot's IV is implausibly low.

## 4. Suggestions — mapping

`suggest(instinct, iv, levels)` returns exactly one of:

| Suggestion | Trigger (priority order) |
|---|---|
| `NO_EDGE_QUIET` | |score| ≤ 14, or lean band with no wall/pin trigger |
| `FAVOR_DEFINED_RISK_PIN` | pinned at the King node |
| `SCALE_EXITS_INTO_RESISTANCE` | at the call wall (score < 40) |
| `ENTRIES_FAVORED_AT_SUPPORT` | at the put wall |
| `CONSIDER_LONG_EXPOSURE` | score ≥ 40 |
| `CONSIDER_DOWNSIDE_PROTECTION` | score ≤ −40 |

Every output carries `experimental: true`, the one-line rationale naming
its drivers (e.g. "pinned at King 760: chop expected…; put skew elevated
(+3.2pts) — downside fear priced"), and the disclaimer.

## 5. Confidence model (enforced in code)

| Component | Confidence | Why |
|---|---|---|
| Structural levels (walls / flip / King from today's chain) | **HIGH** | mechanical measurement of the snapshot |
| Regime label (gamma/vol) | **MEDIUM** | depends on the unobserved dealer-positioning assumption |
| Directional suggestion | **LOW** until ≥20 trading days of recorded signal history (`data/signals/signal_history.json`), then **DATA-DRIVEN** from backtest-measured outcomes | unvalidated until measured |

`confidence_report()` emits these labels for the post-market summary;
`suggest()` stamps the directional label and its reason on every output.

## 6. Assumptions

1. Dealer positioning = net short (inherited from the GEX/VEX engines;
   flippable there, not here).
2. Spot within 0.5% of a wall is "at" the wall; 0.3% of King is "pinned".
3. Sweep proxies carry real directional information once baselines exist
   (today: cold start — flow contributes 0, stated honestly).
4. ATM IV below 2% is implausible for the index capture universe
   (plausibility gate, §3).

## 7. Limitations (honest)

- **Every suggestion is UNVALIDATED / EXPERIMENTAL** until the backtest
  harness scores it on ≥60 trading days of history. The mapping is a
  fixed heuristic, not a finding.
- No historical options data exists yet (forward capture began
  2026-09-20); IV rank and data-driven confidence are gated, not faked.
- Snapshots banked before the 2026-09-20 IV-unit fix carry IVs 100× too
  small — the plausibility gate catches this, but those snapshots'
  Black-Scholes fallback Greeks are also affected.
- Weekend snapshots reflect Friday's close; the summary labels the data
  basis explicitly.
- Only 4 index symbols (SPY, SPX, QQQ, IWM).

## 8. How to run / test

```bash
cd ~/workspace/turblance-trader
PYTHONPATH=src python3 -m unittest discover -s tests   # full suite
python3 scripts/evaluate_alerts.py --force              # one evaluation now
python3 scripts/postmarket_summary.py --date 2026-09-21 --force  # sample
```
