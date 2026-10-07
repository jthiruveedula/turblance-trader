# Turblance Trader — Alerts Spec

**Module:** alert engine (`src/turblance_trader/alerts/`, `scripts/evaluate_alerts.py`) · **Status:** built, unit-tested · **Date:** 2026-09-20
**All code in this module is original, written clean-room.**

---

## 1. What it does

Every 15 minutes during market hours, `scripts/evaluate_alerts.py` loads
the latest banked snapshot per symbol (SPY/SPX/QQQ/IWM), runs the
GEX/flow/VEX/signal engines, and prints `ALERT: ...` lines **only when
something meaningful changed** since the last evaluation. Otherwise it
prints `no alert — quiet` and exits 0. The scheduler relays fired alerts
to chat; quiet runs stay silent.

## 2. Alert triggers (change vs `data/alerts/state.json`)

| # | Trigger | Example |
|---|---|---|
| 1 | Gamma-regime flip | `REGIME FLIP: SPY gamma regime negative -> positive` |
| 2 | Instinct crossing the ±40 bands | `INSTINCT: SPY NEUTRAL -> BULLISH (score 45)` |
| 3 | New sweep-proxy with breadth ≥ 3 strikes | `SWEEP: SPY puts lifted 5 strikes (proxy, experimental)` |
| 4 | Suggestion change | `SUGGESTION: SPY NO_EDGE_QUIET -> ENTRIES_FAVORED_AT_SUPPORT` |
| 5 | Spot crossing a wall or the flip | `LEVEL CROSS: SPY spot crossed above put wall 745` |

Rules:
- The **first evaluation for a symbol establishes the baseline quietly**
  — there is no prior read to diff against, so nothing fires.
- **Dedupe: the same alert never fires twice in one day.** Fired alert
  keys live in the state file; the dedupe resets each trading day.
- Sweep alerts use a stable event key
  (`symbol:expiry:direction:side:strikes`); a persisting cluster alerts
  once, not every 15 minutes.

## 3. Market-hours gate

The script self-gates with the capture module's `is_market_open()`:
Mon–Fri 09:30–16:00 ET (= 08:30–15:00 CT), minus NYSE holidays. Outside
hours it prints `market closed — no evaluation` and exits 0. `--force`
bypasses the gate for testing/backfill.

## 4. Honesty: evaluation cadence vs data cadence

**Evaluations run every 15 minutes, but the engine can only react to new
snapshots.** With the current hourly CBOE delayed feed, effective alert
sensitivity is **hourly** until the IBKR live path exists — a regime flip
at 10:05 is seen at the next hourly snapshot, not at 10:15. The CBOE pull
rate is NOT increased by this script (the ToS politeness compromise
stands). Data latency is ~15 minutes (CBOE delayed) on top of that.

## 5. Bookkeeping the script maintains

- `data/alerts/state.json` — last read per symbol + today's fired keys
  (git-ignored).
- `data/signals/atm_iv.json` — one ATM-IV observation per symbol per day
  (drives the 20-day IV-rank gate; implausible values refused).
- `data/signals/signal_history.json` — one suggestion per symbol per day
  (drives the 20-day directional-confidence gate).

## 6. Schedule

Cron `turblance-signal-alerts` (goal-owned): every 15 minutes, worker runs
`scripts/evaluate_alerts.py`, appends to `logs/alerts.log`, and includes
fired `ALERT:` lines verbatim in its final message for chat delivery.
Quiet runs end silently.

Cron `turblance-postmarket-summary` (goal-owned): daily ~15:30 CT,
weekdays; worker runs `scripts/postmarket_summary.py` and includes the
summary in its final message for chat delivery. The script writes
`data/reports/postmarket_YYYY-MM-DD.md` and skips non-trading days
quietly.

## 7. Limitations (honest)

- Alerts are only as fresh as the snapshots; today that means hourly and
  15-min delayed.
- All alert *interpretations* inherit the engines' EXPERIMENTAL status.
- Sweep alerts are proxies until IBKR tick data confirms them.
- No alert-delivery mechanism beyond the scheduler's chat relay exists
  yet (detection only — the delivery-engine decision is still Jagadeesh's).

## 8. How to run / test

```bash
cd ~/workspace/turblance-trader
PYTHONPATH=src python3 -m unittest discover -s tests -p "test_alerts.py"
python3 scripts/evaluate_alerts.py --force     # evaluate now, ignore hours
python3 scripts/evaluate_alerts.py              # gated run (quiet on Sunday)
```
