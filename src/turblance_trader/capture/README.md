# Forward Options-Chain Capture

Polls free/keyless quote sources on a schedule, normalizes each snapshot to
the shared contract schema, and stores it under `data/chains/`. This is the
"history you can't buy back" pipeline: every trading day captured now is a
day of chain + Greeks history the backtest harness can use later.

**Status:** CBOE delayed adapter live and capturing · IBKR adapter implemented,
awaiting Jagadeesh's gateway setup · Schwab/Alpaca adapters stubbed.

## Quick start

```bash
# One snapshot per symbol, right now (exits quietly outside market hours):
python3 scripts/capture_chains.py --once

# Override symbols / force a capture outside market hours:
python3 scripts/capture_chains.py --once --symbols SPY,SPX --force

# Via the IBKR path (needs a running IB Gateway + ib_async installed):
python3 scripts/capture_chains.py --once --source ibkr

# IBKR path + tick-by-tick trade prints into data/flow/ticks/:
python3 scripts/capture_chains.py --once --source ibkr --capture-ticks

# Flow deltas (data/flow/deltas/) are derived automatically after every
# capture; disable with --no-derive-flow-deltas if ever needed.

# Run the tests (stdlib unittest, no network):
python3 -m unittest discover -s tests -p "test_capture.py"
python3 -m unittest discover -s tests -p "test_flow_capture.py"
```

### Cron (the intended schedule)

US equities trade 09:30–16:00 ET = 08:30–15:00 America/Chicago. The runner
gates on market hours itself (including NYSE holidays), so the cron window
can be generous — the script exits 0 with no snapshot when the market is
closed:

```cron
# Weekdays, every 15 minutes, 8:00–15:00 America/Chicago
*/15 8-15 * * 1-5 /usr/bin/python3 /home/hatch/workspace/turblance-trader/scripts/capture_chains.py --once >> /home/hatch/workspace/turblance-trader/logs/capture.log 2>&1
```

The 15-minute cadence matches the CBOE delayed feed's refresh and keeps us
at ~1 request per symbol per 15 minutes, single-threaded.

## Adapters (`src/turblance_trader/capture/adapters/`)

| Source | Adapter | State | Latency | Fields |
|---|---|---|---|---|
| CBOE delayed quotes | `cboe.py` | **Live** | ~15 min delayed | full chain, IV, Δ/Γ/θ/vega, OI, volume, spot |
| IBKR (TWS API) | `ibkr.py` | Implemented, needs gateway setup | Real-time (with subscriptions) | full chain, IV, Δ/Γ/θ/vega, OI, volume, spot |
| Schwab Trader API | `schwab.py` | Stub | Real-time ($0 w/ account) | raises `SetupRequiredError` with setup steps |
| Alpaca | `alpaca.py` | Stub | Free tier | raises `SetupRequiredError` with setup steps |

All adapters implement `ChainAdapter` (`base.py`): `fetch_raw()` → `parse()` →
`capture()` (fetch + parse + schema validation).

### CBOE adapter notes

- Endpoint: `https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json`
  (index underlyings take a leading underscore: `SPX` → `_SPX`). Free, no key.
- Politeness is enforced in code: sequential requests, ~3s between symbols,
  retries with backoff on timeouts/5xx only. **HTTP 429 or 403 raises
  `RateLimitedError` and the run stops immediately** — no retries, no reroutes.
- Assumptions (marked in code): the payload `timestamp` is US Eastern
  (converted to UTC ISO-8601 for `quote_time`); the per-contract `iv` field
  is quoted in **decimal** (verified 2026-09-20: ATM contracts carried
  0.1012 while the payload's `iv30` read 11.675 in percent) and stored
  as-is. **Snapshots banked before this fix (2026-09-20 and earlier)
  carry IVs 100× too small** — re-capture or rescale before using their
  `implied_volatility` column.

### ⚠️ CBOE terms-of-service caveat (read this)

CBOE's delayed-quotes pages state, verbatim:

> "IT IS STRICTLY PROHIBITED TO DOWNLOAD DELAYED QUOTE TABLE DATA FROM THIS
> WEB SITE BY USING AUTO-EXTRACTION PROGRAMS/QUERIES AND/OR SOFTWARE. CBOE
> WILL BLOCK IP ADDRESSES OF ALL PARTIES WHO ATTEMPT TO DO SO. … DOWNLOADING
> THIS DATA IN ANY OTHER WAY THAN BY MANUAL TICKER SYMBOL ENTRY IS STRICTLY
> PROHIBITED."

This is CBOE's own website terms (seen on cboe.com/delayed_quotes pages).
What that means for us, honestly:

1. The CBOE adapter is the **day-one $0 bootstrap** — it stands up the
   pipeline shape, the schema, and the forward-capture habit *today*.
2. It is **not** the durable production source. CBOE can block automated
   access at any time, and their terms say they will.
3. The durable path is **IBKR** (confirmed account — see "Going live with
   IBKR" below), then Schwab/Alpaca stubs if ever needed.

Nothing here triggers OPRA redistribution obligations: all data stays local
on this machine, nothing is displayed publicly or re-served, and this is
personal research use only.

## Budget / buy ladder

Standing rule: **$0 first, always.** Nothing is purchased or subscribed to
without Jagadeesh's explicit approval. If the CBOE endpoint ever becomes
unusable (blocked, rate-limited, shut off), the fallback ladder is:

1. **IBKR via the local gateway — ~$11.50/mo, inside the approved $20/mo
   budget.** Jagadeesh has the account; he enables the subscriptions himself
   in Client Portal (see "Going live with IBKR"). This is now the designated
   live-data path, not a fallback of last resort.
2. **Schwab Trader API / Tradier — $0 with a brokerage account.** Adapter
   stubs are in place; they only need his account + app registration.
3. **A sub-$20/mo vendor option — only with explicit approval, never
   self-started.** No signups, no trials that convert, no card entry by us.

## Going live with IBKR

**Connection approach (decided 2026-09-20):** `ib_async` (the maintained
fork of `ib_insync`, same API) talking to a locally running **IB Gateway**
over the TWS socket API.

| | TWS socket API + IB Gateway (chosen) | Client Portal Web API (rejected) |
|---|---|---|
| Extra process | IB Gateway (Java, headless-friendly) | Client Portal Gateway (Java) anyway |
| Auth | Once, at gateway login; no API keys | Session expires ~daily → interactive 2FA re-auth |
| Session | Long-lived socket, survives auto-restarts | Hostile to unattended cron |
| Chain + Greeks + OI/vol | `reqSecDefOptParams` → `reqMktData` (generic ticks 100/101/106 + model Greeks) | Possible but same gateway burden |
| Community standard for headless bots | Yes (IBC auto-login/auto-restart) | No |

### Setup steps for Jagadeesh (in order)

1. **Market-data subscriptions** — in Client Portal: **Settings → User
   Settings → Market Data Subscriptions.** Enable:
   - **OPRA (US Options Exchanges), Level 1** — ~$1.50/mo non-pro. Covers
     option quotes, volume, OI.
   - **US Securities Snapshot and Futures Value Bundle** — ~$10/mo. Covers
     the underlying quotes.
   
   Total ≈ **$11.50/mo**, inside the $20/mo approval. Prices are IBKR's
   published non-pro rates — **confirm the exact names and prices in Client
   Portal before subscribing**; allow up to 24h for activation.
   
   Two gotchas to know:
   - IBKR docs state option **Greeks require entitlements for both the
     option (OPRA) *and* the underlying**. If Greeks come back empty on
     SPX/SPY/QQQ after subscribing, the underlying-specific feed is the
     likely missing piece (CBOE Streaming Market Indexes for SPX; the
     Network B bundle for SPY; NASDAQ/UTP for QQQ) — check names/prices in
     the portal.
   - **Testing is free:** IBKR serves *delayed* data with no subscriptions.
     Run the adapter first with `IBKR_MARKET_DATA_TYPE=3` (delayed) to prove
     the pipeline end-to-end before paying anything.

2. **API prerequisites in Client Portal** (both required, both free):
   - Accept the **Market Data API Acknowledgement**: Settings → Account
     Settings → Market Data → API. Without this, API data requests fail
     even with paid subscriptions.
   - Account must be **IBKR Pro** (Lite doesn't support API market data)
     with **≥ $500 equity** to keep subscriptions active.

3. **Install IB Gateway** on the machine that will run captures (this VM or
   another always-on box): download from IBKR's site, log in once, and in
   **Configure → API → Settings** enable *ActiveX and Socket Clients*, set
   the socket port (**4001** live / **4002** paper), and add `127.0.0.1` to
   Trusted IPs.

4. **Headless operation — IBC + IB Key 2FA.** Install
   [IBC](https://github.com/IbcAlpha/IBC) (open-source, drives the gateway
   login and daily auto-restart). In Client Portal → Settings → Security →
   Secure Login System, make **IB Key (IBKR Mobile push)** the 2FA method —
   **not SMS**, which needs a human typing a code on every restart and
   cannot be automated.
   
   **The 2FA friction, honestly:** IBC auto-enters the username/password,
   but IBKR forces a re-login on its daily restart and a full
   re-authentication at its weekly reset (Sunday ~01:00 ET). Each of those
   can push an approval to the phone. Plan on **up to one phone tap per day,
   typically about weekly** — seconds of effort, but it is not zero-touch.
   Keep the IBKR Mobile app installed with notifications on. If a capture
   run fails with auth errors, check the phone first.

5. **Credentials — Secure Vault only.** The socket API needs **no password
   in code** (login happens in the gateway). If gateway credentials are
   ever needed for automation, they go through the Secure Vault flow —
   never chat, never files, never code. This repo will never contain them.

6. **Python dependency** (venv, never system python):
   ```bash
   python3 -m venv ~/workspace/.venv
   ~/workspace/.venv/bin/pip install ib_async pandas numpy
   ```

7. **Environment** (no secrets — just connection coordinates):
   ```bash
   export IBKR_HOST=127.0.0.1
   export IBKR_PORT=4001        # 4002 = paper-trading gateway
   export IBKR_CLIENT_ID=11
   export IBKR_MARKET_DATA_TYPE=1  # 1 = live; use 3 for free delayed testing
   ```
   (Alternatively point `IBKR_CONFIG` at an INI file with an `[ibkr]` section.)

8. **Run and verify:**
   ```bash
   ~/workspace/.venv/bin/python scripts/capture_chains.py --once --source ibkr --symbols SPY
   # expect: "SPY: wrote N contracts, quote_time=... -> data/chains/SPY/..."
   ```

9. **If 2FA re-auth is needed:** approve the IB Key push on the phone, then
   re-run. The adapter fails fast with a clear "could not connect" error —
   it never hangs waiting.

**Operational limits to respect:** IBKR allows ~100 concurrent market-data
lines per username (shared with any open TWS). The adapter requests
contracts in small batches (`IBKR_BATCH_SIZE`, default 60) with pacing
delays (`IBKR_PACING_DELAY`) and cancels each snapshot request when done —
well within limits for 4 symbols × full chains.

## Data schema (the shared contract)

Every snapshot is **one DataFrame** with exactly these columns, in this
order — downstream modules (GEX engine, flow scanner, backtests) code
against this and nothing else:

| Column | Type | Notes |
|---|---|---|
| `symbol` | str | Underlying, e.g. `SPY` (never the `_SPX` CBOE spelling) |
| `quote_time` | str | ISO-8601 UTC of the quote, e.g. `2026-09-18T19:59:00+00:00` |
| `expiry` | str | `YYYY-MM-DD` |
| `strike` | float | |
| `option_type` | str | `call` \| `put` only |
| `bid` / `ask` | float | NaN if unavailable |
| `last` | float | Last trade price, NaN ok |
| `implied_volatility` | float | Decimal (0.185 = 18.5%), NaN ok |
| `open_interest` | int | 0 when unknown |
| `volume` | int | Nullable (session volume) |
| `delta` / `gamma` / `theta` / `vega` | float | Source-model Greeks, NaN ok |
| `spot` | float | Underlying price at `quote_time` |

`validate_schema()` in `schema.py` enforces this on every capture; the
runner refuses to store a snapshot that fails validation.

## Storage

```
data/chains/{symbol}/{YYYY-MM-DD}/snapshot_{HHMMSS}_utc.{csv,parquet}
```

- **Parquet** when `pyarrow`/`fastparquet` is importable, else **CSV with
  identical columns**. No parquet engine is installed in this environment
  today, so snapshots are CSV. Upgrade path: `pip install pyarrow` in a
  venv — `store.py` detects the engine automatically, no code changes.
- Idempotent: the filename derives from the snapshot's own `quote_time`,
  so re-running never duplicates; `--force` overwrites.
- `data/` is git-ignored except `.gitkeep` — snapshots are local research
  data, never committed, never redistributed.

## Flow layer (flow deltas + tick prints)

`src/turblance_trader/capture/flow.py` adds flow-relevant granularity on top
of the chain snapshots, **without any additional polling**:

- **Flow deltas** — `derive_deltas(prev, curr)` is a pure function of two
  consecutive snapshots: per-contract volume/OI diffs matched on
  (expiry, strike, option_type), stored under
  `data/flow/deltas/{symbol}/{date}/deltas_{prev}_to_{curr}_utc.csv`.
  New contracts → `status=new`, expired → `status=expired`.
  Corporate-action-ish patterns are **flagged, never silently absorbed**:
  `oi_jump` (OI up 5x+ with ≥1000 new contracts in one interval) and
  `volume_reset` (session volume *decreasing*, which should not happen
  intraday — means a session boundary, re-listing, or bad print).
- **Tick prints** — the IBKR adapter's `fetch_ticks()` collects trade prints
  via `reqTickByTickData` (`Last`/`AllLast`) into
  `data/flow/ticks/{symbol}/{date}/ticks_{HHMMSS}_utc.csv`.

Both are **source-agnostic**: they read the snapshots the chain pipeline
already banks, so nothing here adds a single CBOE request — the CBOE ToS
caveat above still holds, and the politeness budget (hourly, 429/403 hard
stop) is untouched.

### Wiring

The existing cron does **not** change — delta derivation runs as a post-step
of every chain capture (on by default; `--no-derive-flow-deltas` to skip):

```cron
# unchanged: weekdays, every 15 minutes, 8:00–15:00 America/Chicago
*/15 8-15 * * 1-5 /usr/bin/python3 /home/hatch/workspace/turblance-trader/scripts/capture_chains.py --once >> /home/hatch/workspace/turblance-trader/logs/capture.log 2>&1
```

```bash
# Derive-only catch-up for a banked day (zero network I/O):
python3 - <<'EOF'
import sys; sys.path.insert(0, 'src')
from turblance_trader.capture.flow import derive_day_deltas
for symbol in ["SPY", "SPX", "QQQ", "IWM"]:
    print(symbol, derive_day_deltas(symbol, "2026-09-21", "data"))
EOF
```

No cron was added for this layer: the derive step rides the existing chain
cron, and any future tick-capture cron must do its own network only against
IBKR (never CBOE).

### What changes when IBKR goes live

1. **Finer chain cadence.** The same cron with `--source ibkr` (e.g. every
   5 minutes during the session instead of 15) yields finer deltas
   automatically — `derive_day_deltas` just diffs consecutive snapshots,
   whatever the cadence. Suggested, not yet scheduled:
   `*/5 8-15 * * 1-5 .../capture_chains.py --once --source ibkr`.
   IBKR pacing is handled inside the adapter (batches of 60, cancel-after-
   snapshot), and IBKR 429-style throttling surfaces as a clean failure,
   not a retry storm.
2. **Real tick prints.** Add `--capture-ticks` to an IBKR run:
   `capture_chains.py --once --source ibkr --capture-ticks`. Prints land in
   `data/flow/ticks/` for the flow scanner's tape analysis. Requires the
   OPRA subscription from the setup list above — in delayed test mode
   (`IBKR_MARKET_DATA_TYPE=3`) tick-by-tick is not served, and the path
   no-ops cleanly instead of failing the run.
3. **Delta source switches transparently.** Deltas are derived from whatever
   snapshots exist in `data/chains/` — CBOE-hourly today, IBKR-fine-grained
   tomorrow. No code change, no re-ingestion.

**Status:** delta derivation is live and tested (pure local computation).
The tick path is implemented against the TWS API and unit-tested with
fakes, but has **never run against a real gateway** — treat the first live
`--capture-ticks` run as a smoke test, not production data.

## What's stubbed / not yet built

- **Schwab / Alpaca adapters** (`schwab.py`, `alpaca.py`): raise
  `SetupRequiredError` with the exact account/app steps Jagadeesh would
  need. No credentials are requested or accepted anywhere in this repo.
- **IBKR adapter** (`ibkr.py`): fully implemented against the TWS API, but
  **no live connection has been attempted** — there are no credentials in
  this environment, and `ib_insync`/`ib_async` isn't installed. Unit tests
  cover it via a fake gateway. First live run happens only after Jagadeesh
  completes the "Going live with IBKR" steps above.
- Out of scope by design: public APIs, alert engines, real-money order
  routing (research + paper trading only).

## Exit codes (`scripts/capture_chains.py`)

| Code | Meaning |
|---|---|
| 0 | Success, or cleanly skipped outside market hours |
| 2 | Usage error, unknown source, or adapter setup not complete |
| 3 | Source blocked us (HTTP 429/403) — **hard stop, investigate** |
| 4 | Fetch/parse/schema failure for ≥1 symbol |
| 5 | Snapshot write failure |
