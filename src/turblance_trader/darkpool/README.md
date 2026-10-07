# Dark Pool / ATS Module

Historical off-exchange (dark pool) analytics for turblance-trader, plus a
stub for real-time TRF prints. All original code; public methodology only.

**Status:** FINRA ATS weekly adapter implemented (not yet run against the
live endpoint — first pull happens on the weekly cron) · analytics pure
functions with unit tests · TRF-live adapter stubbed pending the IBKR
gateway.

> **PERSONAL RESEARCH USE ONLY — FINRA terms compliance.** FINRA publishes
> ATS Transparency data for non-commercial personal or professional use.
> This module downloads that data for Jagadeesh's own research, keeps it
> local on this machine (`data/darkpool/`, git-ignored), and never
> republishes, re-serves, or displays it to any third party. Interpretive
> outputs (dark-volume shares, concentration) are labeled experimental.

## Why FINRA ATS data

Recon finding: real-time off-exchange prints live inside the consolidated
SIP tape; every retail "dark pool API" just repackages tape/TRF data. The
$0 path is therefore:

- **(a) FINRA ATS Transparency data** (`otctransparency.finra.org`) — weekly
  per-security/per-ATS share volume and trade counts, ~2 weeks delayed for
  Tier 1 NMS stocks (4 weeks for Tier 2/OTCE), machine-downloadable. This
  is the historical-analytics source, live now.
- **(b) Real-time TRF prints via the IBKR adapter** — architecture is ready
  (`trf_live.py` stub); it wires up when Jagadeesh's gateway goes live.

## Quick start

```bash
# Pull the latest fully-published Tier 1 week (run at most weekly):
python3 scripts/fetch_ats.py

# A specific report week, or force a re-download:
python3 scripts/fetch_ats.py --week 2026-08-31
python3 scripts/fetch_ats.py --week 2026-08-31 --force

# Run the tests (stdlib unittest, no network):
python3 -m unittest discover -s tests -p "test_darkpool.py"
```

### Cron (the intended schedule)

The adapter enforces a minimum 7-day gap between pulls itself
(`CadenceError`), so the cron window can be generous:

```cron
# Sundays, 09:00 America/Chicago — pulls the latest published week, if new
0 9 * * 0 /usr/bin/python3 /home/hatch/workspace/turblance-trader/scripts/fetch_ats.py >> /home/hatch/workspace/turblance-trader/logs/ats.log 2>&1
```

## The FINRA endpoint & format (researched 2026-09-20)

Source: FINRA *"OTC Transparency (ATS and Non-ATS) — API Specifications
File Download"*, v0.4 (Jan 2019):

- Spec PDF:
  <https://www.finra.org/sites/default/files/OTC-Transparency-Data-File-Download-API-v04.pdf>
- Endpoint:
  `POST https://api.finra.org/data/group/otcMarket/name/weeklySummary`
  with `Content-Type: application/json`, `Accept: application/json`, and a
  filter body such as:
  ```json
  {"compareFilters": [
     {"compareType": "EQUAL", "fieldName": "summarytypecode",
      "fieldValue": "ATS_W_SMBL_FIRM"},
     {"compareType": "EQUAL", "fieldName": "weekstartdate",
      "fieldValue": "2026-08-31"}],
   "limit": 50000}
  ```
  (`ATS_W_SMBL_FIRM` = per-security / per-ATS weekly grain.)
- Response: JSON array of row objects with the spec's data-dictionary
  fields: `issueSymbolIdentifier`, `issueName`, `marketParticipantIdentifier`
  (the 4-char ATS MPID), `marketParticipantName`, `tierIdentifier` (T1/T2/OTCE),
  `summaryStartDate`, `totalWeeklyTradeCount`, `totalWeeklyShareQuantity`,
  `productTypeCode`, `summaryTypeCode`, `weekStartDate` (Monday partition
  key), `lastUpdateDate`, `initialPublishedDate`, `lastReportedDate`.
  Field casing varies across spec revisions; the parser matches keys
  case-insensitively with documented aliases.
- ATS Blocks summaries (monthly, per-venue block stats) use bucket codes
  `2K` (2K–<10K shares), `10K` (10K+ shares), `200K` ($200K+),
  `10K-200K` (10K+ AND $200K+), `100K` ($100K–<$200K),
  `2K-100K` (2K–<10K AND $100K–<$200K). The v0.4 spec does not publish a
  machine endpoint URI for the blocks summaries, so they are modeled in the
  schema (`block_bucket`) and analytics but not fetched yet.
- Publication: weekly files available no earlier than 6:00 AM ET on
  business days; Tier 1 = 2-week delay, Tier 2/OTCE = 4-week delay.

## Row schema (the ATS contract)

Every weekly snapshot is **one DataFrame** with exactly these columns, in
this order (`schema.py`; validated by `validate_ats_schema()`):

| Column | Type | Notes |
|---|---|---|
| `week_start` | str | `YYYY-MM-DD` Monday of the report week (partition key) |
| `symbol` | str | Issue symbol, e.g. `SPY` |
| `issue_name` | str | Nullable |
| `ats_mpid` | str | 4-char venue id, e.g. `AQUA` |
| `ats_name` | str | Nullable |
| `tier` | str | `T1` \| `T2` \| `OTCE` |
| `weekly_shares` | int | ≥ 0 |
| `weekly_trades` | int | ≥ 0 |
| `block_bucket` | str | Bucket code, or null for weekly rows |
| `last_updated` | str | `YYYY-MM-DD`, nullable |
| `source` | str | Provenance, e.g. `finra_ats` |

## Storage

```
data/darkpool/{YYYY-MM-DD}.{csv,parquet}
```

- **Parquet** when `pyarrow`/`fastparquet` is importable, else **CSV with
  identical columns**. No parquet engine is installed in this environment
  today, so weekly files are CSV.
- Idempotent: the filename derives from the report week; re-running never
  duplicates; `--force` overwrites.
- `data/` is git-ignored — FINRA downloads are local research data, never
  committed, never redistributed.

## Analytics (`analytics.py`)

Pure functions, NaN-safe (missing inputs → NaN, never exceptions or
infinities). Interpretive outputs are marked experimental in docstrings:

| Function | What it computes |
|---|---|
| `weekly_symbol_totals` | Per (week, symbol): ATS shares/trades, venue count, avg trade size |
| `ats_share_within_symbol` | Each venue's share of its symbol's ATS volume that week |
| `concentration` | HHI + top-1/top-3 venue share per (week, symbol) — experimental |
| `ats_market_share` | Each venue's share of total ATS volume that week — experimental |
| `week_over_week` | Fractional + absolute WoW change per (symbol, venue); NaN when undefined |
| `dark_volume_share` | Per-symbol dark share of total — needs a `consolidated` volume input (the future TRF wiring point); returns NaN + an honest note without it — experimental |
| `block_bucket_summary` | Block-bucket mix from ATS-Blocks rows; empty frame when the data doesn't support it — experimental |

## Real-time TRF stub (`trf_live.py`)

`TrfLiveAdapter` mirrors the `DarkPoolAdapter` interface but raises
`SetupRequiredError` (with the exact IBKR setup steps) from every method
until the gateway is live. The future wiring point is documented in the
module docstring: `ib_async` `reqTickByTickData(tickType="AllLast")`,
filter ticks to the TRF exchange identifier, map each
`TickByTickAllLast` to a print row (a new `TRF_PRINT_SCHEMA`, print-level,
not weekly-aggregate), and aggregate weekly consolidated volume per symbol
into `analytics.dark_volume_share(consolidated=...)`.

## Budget / constraints

**$0-first, always.** No subscriptions, no paid APIs, no credentials
anywhere in this package. No real-money routing. No alert-delivery
mechanism. Politeness is enforced in code: one request per weekly pull,
minimum 7-day gap between pulls, **HTTP 429/403 = hard stop** (no retry,
no reroute).

## Limitations (honest)

1. **Delayed:** Tier 1 ATS data is ~2 weeks old by design; it cannot drive
   intraday decisions — only historical structure research and backtests.
2. **ATS sample only:** FINRA ATS data covers ATS prints, not all
   off-exchange volume (e.g. internalized retail flow at wholesalers is
   reported differently). Any "dark share" computed without consolidated
   volume is explicitly NaN, not estimated.
3. **Blocks endpoint URI unpublished:** the v0.4 spec documents the blocks
   bucket codes and fields but not a machine endpoint URI, so block-bucket
   analytics are schema/analytics-ready but unfed until the URI is
   confirmed.
4. **Real-time path needs the gateway:** `trf_live` is a stub until
   Jagadeesh completes the IBKR setup in `capture/README.md`.
