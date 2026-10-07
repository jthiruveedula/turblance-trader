# Turblance Trader — GEX Engine Spec

**Module:** dealer-exposure (GEX) engine · **Status:** built, unit-tested, E2E-verified on live data · **Date:** 2026-09-20
**All code in this module is original, written clean-room.** Public methodology knowledge (textbook option math, publicly described market-structure concepts) informed the design; no third-party code, text, or proprietary implementation was copied.

---

## 1. What it does

Turns an options chain snapshot into a dealer-positioning profile: net gamma exposure per strike and per expiry, plus the key levels traders read off it — zero-gamma flip, call/put walls, King node, and a gamma-regime label. A backtest harness consumes these profiles to validate GEX-derived signals against the evidence standard in `backtests/README.md`.

Pipeline: `capture` (chain snapshots → Parquet/CSV) → `gex` (exposure profile) → `backtests/harness` (signal validation).

## 2. Methodology and formulas

### 2.1 Gamma (Black-Scholes)

Per-contract gamma comes from the vendor when available; otherwise it is computed from implied volatility with the public Black-Scholes formula (identical for calls and puts):

- `d1 = [ln(S/K) + (r + σ²/2)·T] / (σ·√T)`
- `Γ = N'(d1) / (S · σ · √T)`, where `N'` is the standard-normal pdf.

Edge policy: `T ≤ 0`, `σ ≤ 0`, `S ≤ 0`, `K ≤ 0`, or NaN inputs → gamma `0` (never NaN leaks). Time to expiry `T = (expiry_date − quote_date).days / 365`, clipped at 0.

### 2.2 Dollar gamma exposure (GEX)

Per contract row, in **dollars per 1% move** of the underlying:

```
GEX = Γ × open_interest × 100 × S² × 0.01
```

(`100` = shares per contract; `S² × 0.01` converts per-$1 gamma to per-1%-move.)

### 2.3 Dealer-positioning convention (explicit assumption)

Customers are assumed long options, so **dealers are short**: `dealer_gex = −1 × customer_gex`. This is a modeling simplification (we cannot see actual dealer inventory), so it is a *parameter*, not a hidden constant: `compute_profile(df, dealer_position='short'|'long'|'flat')`. The backtest layer can flip it for sensitivity analysis.

### 2.4 Aggregation

- **by_strike:** `strike | call_gex | put_gex | net_gex` (dealer convention applied), summed across expiries.
- **by_expiry:** `expiry | net_gex`.

### 2.5 Key levels (definitions)

- **Zero-gamma flip:** the strike where total net dealer gamma changes sign. Only strikes with meaningful exposure (`|net_gex| ≥ 1e-9`) participate — far-tail strikes with no open interest carry zero GEX and must not anchor the crossing (a leading run of zeros is not a flip). First strict sign change among qualifying strikes, ascending, linearly interpolated. `None` when exposure never changes sign. *Interpretation is experimental/unvalidated.*
- **Call wall:** strike at/above spot with maximum `|call_gex|` (overhead zone). **Put wall:** strike at/below spot with maximum `|put_gex|` (support zone). `None` when no strike qualifies.
- **King node:** strike with maximum `|net_gex|` overall — the "center of structural gravity" of the chain.
- **Gamma regime:** `total_net_gex / Σ|net_gex|` with ±0.5 cutoffs → `positive` (range-day heuristic), `negative` (trend-day heuristic), else `mixed`. **Experimental, unvalidated** — labeled as such in code.
- **GEX velocity:** per-strike change in net GEX between two snapshots (`gex_velocity(a, b)`), for rate-of-change / accumulation analysis.

### 2.6 Public API

```python
from turblance_trader.gex import compute_profile, gex_velocity
profile = compute_profile(df, spot=None, risk_free_rate=0.0, dealer_position='short')
profile.by_strike        # DataFrame: strike, call_gex, put_gex, net_gex
profile.by_expiry        # DataFrame: expiry, net_gex
profile.spot             # float
profile.zero_gamma_flip  # float | None
profile.call_wall        # float | None
profile.put_wall         # float | None
profile.king_node        # {'strike', 'net_gex'}
profile.regime           # 'positive' | 'negative' | 'mixed'
profile.to_dict()        # JSON-serializable summary
profile.to_dataframe()   # the by_strike frame
```

Input is a DataFrame following the capture schema (16 columns; see `src/turblance_trader/capture/README.md`). Rows missing both gamma and IV are skipped and counted (`skipped_rows`).

---

## 3. Data sources

| Source | State | Data | Cost |
|---|---|---|---|
| CBOE delayed quotes | **Live now** (forward capture started 2026-09-20) | 15-min delayed chains, IV + Greeks + OI, no key | $0 |
| IBKR (TWS socket API + IB Gateway, `ib_async`) | Adapter built & unit-tested; **needs Jagadeesh's setup** (see §3.2) | Real-time chains + Greeks once subscriptions active; delayed free for testing | ~$11.50/mo after his setup |
| Schwab / Alpaca | Stubs (setup instructions only) | — | $0 with account |

**Budget:** Jagadeesh approved up to **$20/mo** for real-time GEX/VEX/flow data. Standing rule: **$0-first** — do not subscribe to anything without his explicit approval, and only propose spending if the free path proves insufficient.

### 3.1 CBOE — ToS caveat (important)

CBOE's delayed-quotes terms state automated downloading is **strictly prohibited** and offending IPs will be blocked. The CBOE adapter is therefore the **day-one bootstrap, not the durable source**: it polls politely (single-threaded, ~3s between symbols), and the code treats HTTP 429/403 as a **hard stop** (no retry, no reroute). The scheduled capture runs **hourly** (not every 15 min) as a compromise between history-building and politeness. **Decision for Jagadeesh:** keep the hourly CBOE cron, dial it back, or pause it entirely once IBKR is live.

### 3.2 IBKR — setup needed from Jagadeesh (in order)

1. **Market-data subscriptions** (Client Portal → Settings → User Settings → Market Data Subscriptions):
   - **OPRA (US Options Exchanges), Level 1** — ~$1.50/mo non-pro
   - **US Securities Snapshot and Futures Value Bundle** — ~$10/mo
   - ≈ **$11.50/mo total**, inside the $20 approval. **Confirm exact names/prices in the portal before subscribing**; allow 24h for activation. Gotcha: IBKR requires entitlements for *both* the option (OPRA) *and* the underlying before Greeks are returned — if Greeks come back empty, the underlying-specific feed is the likely missing piece.
   - **Free first:** delayed data works with no subscriptions — run the adapter with `IBKR_MARKET_DATA_TYPE=3` to prove it end-to-end before paying.
2. **API prerequisites** (both free): accept the **Market Data API Acknowledgement** (Settings → Account Settings → Market Data → API); account must be **IBKR Pro** (not Lite) with **≥ $500 equity**.
3. **Install IB Gateway** on the always-on machine; Configure → API → Settings: enable socket clients, port **4001** (live) / 4002 (paper), trust `127.0.0.1`.
4. **IBC + IB Key (phone push) 2FA** — not SMS. Honest friction: IBKR forces re-login on daily restart and full re-auth at the weekly Sunday ~01:00 ET reset → **up to one phone tap per day, typically about weekly**.
5. **Credentials via Secure Vault only** — never chat, files, or code. (The socket API needs no password in code; login happens in the gateway.)
6. `python3 -m venv ~/workspace/.venv && ~/workspace/.venv/bin/pip install ib_async pandas numpy`
7. Set `IBKR_HOST / IBKR_PORT / IBKR_CLIENT_ID / IBKR_MARKET_DATA_TYPE` (or `IBKR_CONFIG` INI).
8. Verify: `python scripts/capture_chains.py --once --source ibkr --symbols SPY`
9. If a run fails with auth errors, check the phone for the IB Key push first.

Full detail: `src/turblance_trader/capture/README.md` §"Going live with IBKR".

### 3.3 Licensing boundary

OPRA charges ~$1,500/mo redistribution (even for delayed data) and exchanges charge for display — **none of this applies here**: all data stays local, personal research use, nothing displayed publicly. Historical-only data is OPRA-exempt. If turblance-trader ever serves data to other users, the licensing picture changes completely — that is a pre-launch decision, not a Phase-1 concern.

---

## 4. Assumptions

1. Dealers are net short all customer-long options (flippable parameter; §2.3).
2. Open interest proxies positioning; zero-OI strikes contribute zero exposure.
3. 100 shares per contract, uniformly (SPX included — documented; revisit if contract specs differ).
4. `T` in calendar days / 365; risk-free rate defaults to 0; dividends ignored (`q = 0`) — small error on dividend-paying underlyings (SPY/QQQ), flagged for refinement.
5. American-exercise effects on gamma ignored (standard approximation; small except very near expiry / deep ITM puts).
6. Vendor Greeks (CBOE) accepted as computed; Black-Scholes fallback only fills gaps.
7. Spot defaults to the median of the snapshot's `spot` column.

---

## 5. Limitations (honest)

- **No historical options data exists yet** — forward capture began 2026-09-20. Every GEX-derived *signal* is **UNVALIDATED / EXPERIMENTAL** until re-run on real history (trigger: ≥60 trading days of snapshots; `python3 backtests/harness/run_validation.py --symbol SPY`).
- CBOE data is 15-min delayed; weekend snapshots reflect Friday's close (no Sunday trading).
- The regime heuristic (±0.5 cutoffs), wall/flip *interpretations*, and tap-decay probabilities are hypotheses, not findings.
- Tap-decay validation additionally needs an intraday price series (planned via IBKR).
- Hourly CBOE snapshots are coarse for velocity analysis; IBKR real-time will supersede.
- Only 4 index symbols (SPY, SPX, QQQ, IWM) in the capture universe so far.

---

## 6. Validation status

- **Unit tests:** 71/71 pass (`python3 -m unittest discover -s tests`) — 31 capture, 19 engine (incl. a regression test for the zero-exposure-tail flip artifact found during integration), 21 backtest harness. Stdlib only, no network in tests.
- **E2E verified** on a real SPY snapshot (12,388 contracts, 2026-09-18 close): loader → engine → all three signals run cleanly (King 760.0 vs spot 761.69, regime negative, no spurious flip).
- **Backtest reports** (`backtests/reports/gex-king-magnet.md`, `gex-tap-decay.md`, `gex-regime.md`) follow the evidence standard; each concludes **UNVALIDATED — EXPERIMENTAL** with zero fabricated numbers and a concrete re-run trigger.
- **Forward capture:** cron `turblance-chain-capture` (hourly, goal-owned) is live; 4 snapshots banked 2026-09-20.

## 7. How to run

```bash
cd ~/workspace/turblance-trader
python3 -m unittest discover -s tests            # full suite
python3 scripts/capture_chains.py --once --force # manual snapshot (all symbols, CBOE)
python3 scripts/capture_chains.py --once --source ibkr --symbols SPY  # once IBKR is set up
python3 backtests/harness/run_validation.py --symbol SPY  # exits 2 until ≥60 days of data
```

## 8. What's next (recommended)

1. **Jagadeesh completes IBKR setup** (§3.2) → switch capture source to `ibkr`, keep CBOE as fallback.
2. **VEX (vega exposure) engine** — same pipeline, vega column already captured; extends the regime/level toolkit.
3. **Options-flow scanner** (already Jagadeesh's likely second module) — needs its own forward capture (flow prints), start early for the same "history you can't buy back" reason.
4. Re-run validation monthly as forward history accumulates; buy Thetadata/ORATS backfill only with explicit approval if the wait is unacceptable.
