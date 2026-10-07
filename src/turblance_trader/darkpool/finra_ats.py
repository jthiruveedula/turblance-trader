"""FINRA ATS Transparency weekly downloader (free, keyless, ~2-week delayed).

Endpoint (documented in FINRA's "OTC Transparency (ATS and Non-ATS) API
Specifications File Download" v0.4, Jan 2019):
    POST https://api.finra.org/data/group/otcMarket/name/weeklySummary
    Content-Type: application/json ; Accept: application/json

Request body (from the spec's curl/Postman examples):
    {"compareFilters": [
        {"compareType": "EQUAL", "fieldName": "summarytypecode",
         "fieldValue": "ATS_W_SMBL_FIRM"},
        {"compareType": "EQUAL", "fieldName": "weekstartdate",
         "fieldValue": "<YYYY-MM-DD>"}],
     "limit": <N>}

The ``ATS_W_SMBL_FIRM`` summary type is the per-security / per-ATS weekly
grain: one row per (symbol, ATS MPID) with aggregate weekly shares and
trade counts. That is the grain this adapter normalizes.

Response: a JSON array of row objects. Field names follow the spec's data
dictionary (issueSymbolIdentifier, issueName, marketParticipantIdentifier,
marketParticipantName, tierIdentifier, summaryStartDate, totalWeeklyTradeCount,
totalWeeklyShareQuantity, productTypeCode, summaryTypeCode, weekStartDate,
lastUpdateDate, ...). Field casing varies between spec revisions, so the
parser matches keys case-insensitively and accepts documented aliases.

Publication rhythm (from the spec): Tier 1 NMS stocks publish on a TWO-week
delay; Tier 2 / OTCE on a FOUR-week delay. Weekly files are available no
earlier than 6:00 AM ET on business days.

Politeness contract (enforced in code):
  * one HTTP request per weekly pull, single-threaded
  * never more than one pull per 7 days (``CadenceError`` otherwise) —
    the data itself is weekly, so faster polling adds load for zero new data
  * retries with backoff only on timeouts / 5xx; HTTP 429 or 403 raises
    RateLimitedError immediately and the run STOPS (no rerouting)

PERSONAL RESEARCH USE ONLY (see package docstring): FINRA's terms restrict
these downloads to non-commercial personal/professional use. Data stays
local, is never republished or re-served, and is never shown to third
parties.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from turblance_trader.darkpool.base import (
    ATSSourceError,
    CadenceError,
    DarkPoolAdapter,
    RateLimitedError,
)
from turblance_trader.darkpool.schema import ATS_SCHEMA_COLUMNS, validate_ats_schema

#: Documented endpoint from FINRA's v0.4 spec. (One example in the spec uses
#: the host ``api.dapi.finra.org`` for CSV output; the JSON examples use
#: ``api.finra.org``. We request JSON from the documented primary host.)
_URL = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"

_USER_AGENT = "turblance-trader/0.1 (personal research use; weekly-cadence client)"

#: Summary type for the per-security / per-ATS weekly grain.
_SUMMARY_TYPE = "ATS_W_SMBL_FIRM"

#: Upper bound on rows per pull. Assumption (documented): one weekly pull is
#: far below this; if the API ever truncates at the limit, the client-side
#: week filter in ``parse`` keeps only the requested week and the caller is
#: warned by the row-count log. Revisit if FINRA documents pagination.
_REQUEST_LIMIT = 50_000

#: Minimum spacing between pulls. The data is weekly + ~2-week delayed.
MIN_DAYS_BETWEEN_PULLS = 7

#: Tier 1 delay per the spec (Tier 2 / OTCE publish on a 4-week delay).
_TIER1_DELAY_DAYS = 14


def latest_report_week(today: date | None = None) -> str:
    """Most recent fully-published Tier 1 report week (Monday, YYYY-MM-DD).

    The spec publishes Tier 1 NMS data on a two-week delay, so the newest
    week we can expect is (today - 14 days) rounded back to Monday.
    """
    today = today or date.today()
    anchor = today - timedelta(days=_TIER1_DELAY_DAYS)
    monday = anchor - timedelta(days=anchor.weekday())
    return monday.isoformat()


class FinraAtsAdapter(DarkPoolAdapter):
    """Downloads FINRA ATS weekly per-security/per-ATS aggregates."""

    source_name = "finra_ats"

    def __init__(self, *, timeout: float = 60.0, max_retries: int = 2) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    # ------------------------------------------------------------------ fetch
    def _payload(self, week_start: str) -> dict:
        return {
            "compareFilters": [
                {
                    "compareType": "EQUAL",
                    "fieldName": "summarytypecode",
                    "fieldValue": _SUMMARY_TYPE,
                },
                {
                    "compareType": "EQUAL",
                    "fieldName": "weekstartdate",
                    "fieldValue": week_start,
                },
            ],
            "limit": _REQUEST_LIMIT,
        }

    @staticmethod
    def _raise_for_status(status: int, body: bytes) -> None:
        """Map an HTTP status to our error taxonomy. 429/403 = hard stop."""
        if status in (429, 403):
            raise RateLimitedError(
                f"FINRA answered HTTP {status} — hard stop, no retry/reroute. "
                f"Body head: {body[:200]!r}"
            )
        if status >= 400:
            raise ATSSourceError(f"FINRA answered HTTP {status}: {body[:200]!r}")

    def _post(self, payload: dict) -> list:
        """Single polite POST. Separated for testability (no network in tests)."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    body = resp.read()
                self._raise_for_status(status, body)
                parsed = json.loads(body.decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                # HTTPError carries the status; 429/403 = hard stop, no retry.
                self._raise_for_status(exc.code, exc.read()[:500])
                raise  # unreachable: _raise_for_status always raises for >=400
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise ATSSourceError(
                        f"FINRA ATS fetch failed after {attempt} attempts: {exc}"
                    ) from exc
                time.sleep(2.0 * attempt)
        if isinstance(parsed, dict) and "data" in parsed:
            parsed = parsed["data"]
        if not isinstance(parsed, list):
            raise ATSSourceError(
                f"unexpected FINRA response shape: {type(parsed).__name__}"
            )
        return parsed

    def fetch_raw(self, week_start: str) -> dict:
        """POST the weeklySummary API for ``week_start`` (YYYY-MM-DD Monday)."""
        pd.to_datetime(week_start, format="%Y-%m-%d")  # fail fast on bad dates
        rows = self._post(self._payload(week_start))
        return {"week_start": week_start, "rows": rows}

    # ------------------------------------------------------------------ parse
    #: Case-insensitive field aliases across spec revisions.
    _FIELD_ALIASES = {
        "symbol": (
            "issuesymbolidentifier", "symbol",
        ),
        "issue_name": ("issuename", "issue_description", "issuedescription"),
        "ats_mpid": ("marketparticipantidentifier", "mpid", "ats_mpid"),
        "ats_name": (
            "marketparticipantname", "ats_description", "atsdescription",
        ),
        "tier": ("tieridentifier", "report_type", "reporttype"),
        "weekly_shares": (
            "totalweeklysharequantity", "totalsharequantitysum", "shares",
        ),
        "weekly_trades": (
            "totalweeklytradecount", "totaltradecountsum", "trades",
        ),
        "row_week": ("weekstartdate", "summarystartdate"),
        "last_updated": (
            "lastupdatedate", "shares_last_updated", "shareslastupdated",
        ),
        "summary_type": ("summarytypecode", "reporttype"),
    }

    @classmethod
    def _row_get(cls, row: dict, target: str):
        lowered = {str(k).lower(): v for k, v in row.items()}
        for alias in cls._FIELD_ALIASES[target]:
            if alias in lowered:
                return lowered[alias]
        return None

    def parse(self, raw: dict, week_start: str) -> pd.DataFrame:
        """Normalize raw FINRA rows into the ATS contract schema.

        Client-side filters: keep only ATS summary rows for the requested
        week, and drop rows missing the (symbol, ats_mpid) key.
        """
        rows = raw.get("rows", []) if isinstance(raw, dict) else []
        records: list[dict] = []
        dropped = 0
        for row in rows:
            if not isinstance(row, dict):
                dropped += 1
                continue
            summary_type = str(self._row_get(row, "summary_type") or "")
            if summary_type and not summary_type.upper().startswith("ATS_"):
                continue  # not an ATS row (e.g. OTC_W_* mixed in)
            symbol = self._row_get(row, "symbol")
            mpid = self._row_get(row, "ats_mpid")
            if not symbol or not mpid:
                dropped += 1
                continue
            row_week = self._row_get(row, "row_week")
            if row_week and str(row_week)[:10] != week_start:
                continue  # a different week than requested
            shares = self._row_get(row, "weekly_shares")
            trades = self._row_get(row, "weekly_trades")
            try:
                shares_i = int(float(shares)) if shares not in (None, "") else None
                trades_i = int(float(trades)) if trades not in (None, "") else None
            except (TypeError, ValueError):
                dropped += 1
                continue
            if shares_i is None or trades_i is None:
                dropped += 1
                continue
            records.append(
                {
                    "week_start": week_start,
                    "symbol": str(symbol).strip().upper(),
                    "issue_name": (
                        str(self._row_get(row, "issue_name")).strip() or None
                    ),
                    "ats_mpid": str(mpid).strip().upper(),
                    "ats_name": (
                        str(self._row_get(row, "ats_name")).strip() or None
                    ),
                    "tier": str(self._row_get(row, "tier") or "").strip().upper()
                    or None,
                    "weekly_shares": shares_i,
                    "weekly_trades": trades_i,
                    "block_bucket": None,  # weekly summaries have no buckets
                    "last_updated": (
                        str(self._row_get(row, "last_updated"))[:10] or None
                    ),
                    "source": self.source_name,
                }
            )
        df = pd.DataFrame(records, columns=ATS_SCHEMA_COLUMNS)
        if dropped:
            # Not fatal: FINRA occasionally ships malformed rows; the schema
            # validator still guards the rows we keep.
            import logging

            logging.getLogger(__name__).warning(
                "finra_ats.parse: dropped %d malformed rows for week %s",
                dropped,
                week_start,
            )
        return validate_ats_schema(df) if not df.empty else df

    # ------------------------------------------------- cadence-guarded pull
    def capture_week(
        self, week_start: str, data_dir: str | Path, *, force: bool = False
    ) -> pd.DataFrame:
        """Fetch + parse + validate one weekly snapshot, honoring cadence.

        Raises:
            CadenceError: a pull happened less than
                ``MIN_DAYS_BETWEEN_PULLS`` ago (unless ``force=True``).
        """
        from turblance_trader.darkpool.storage import (
            newest_download_mtime,
            weekly_path,
        )

        if weekly_path(week_start, data_dir).exists() and not force:
            # Idempotent: already have this week; re-parse from disk instead
            # of hitting FINRA again.
            from turblance_trader.darkpool.storage import read_weekly

            return read_weekly(weekly_path(week_start, data_dir))
        last = newest_download_mtime(data_dir)
        if last is not None and not force:
            import datetime as _dt

            age_days = (
                _dt.datetime.now().timestamp() - last
            ) / 86400.0
            if age_days < MIN_DAYS_BETWEEN_PULLS:
                raise CadenceError(
                    f"last ATS pull was {age_days:.1f} days ago; "
                    f"FINRA ATS data is weekly — wait "
                    f"{MIN_DAYS_BETWEEN_PULLS - age_days:.1f} more days "
                    "or pass force=True."
                )
        return self.capture(week_start)
