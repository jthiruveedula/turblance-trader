"""CBOE delayed-quotes adapter (free, keyless, ~15-min delayed).

Endpoint pattern (undocumented public JSON served by CBOE's own CDN):
    https://cdn.cboe.com/api/global/delayed_quotes/options/{CBOE_SYMBOL}.json
Index underlyings take a leading underscore (``SPX`` -> ``_SPX``); ETF
underlyings (SPY/QQQ/IWM) use the plain ticker.

Politeness contract (enforced in code):
  * single-threaded, sequential, ~3s pause between symbols
  * at most one request per symbol per 15 minutes (the cron schedule)
  * retries with backoff only on timeouts / 5xx; HTTP 429 or 403 raises
    RateLimitedError immediately and the run STOPS (no rerouting).

IMPORTANT ToS CAVEAT (see README.md for the full note):
CBOE's delayed-quotes pages state that downloading quote data "by using
auto-extraction programs/queries and/or software" is strictly prohibited and
that CBOE will block offending IP addresses. This adapter exists to stand up
the pipeline shape against the documented JSON; for ongoing scheduled
capture the compliant $0 path is the Schwab or Alpaca adapter (stubs in this
package) once Jagadeesh wires up his accounts.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from turblance_trader.capture.adapters.base import (
    ChainAdapter,
    ChainSourceError,
    RateLimitedError,
)

# Assumption (documented): the "timestamp" string in CBOE's payload is US
# Eastern. CBOE is a US venue and the values line up with ET market hours.
CBOE_TZ = ZoneInfo("America/New_York")

#: CBOE ticker spelling per underlying. Only SPX needs the underscore prefix
#: among the starter set; extend here for more index underlyings.
CBOE_SYMBOL_MAP = {
    "SPX": "_SPX",
}

_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{cboe_symbol}.json"
_USER_AGENT = (
    "turblance-trader/0.1 (personal research use; single daily-run client)"
)

# OCC-style contract id: <root><YYMMDD><C|P><strike:8 digits, /1000>.
# e.g. "SPXW260918C06600000" -> 2026-09-18 call @ 6600.00
_CONTRACT_RE = re.compile(r"(\d{6})([CP])(\d{8})$")


class CboeDelayedAdapter(ChainAdapter):
    source_name = "cboe"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        pause_between_requests: float = 3.0,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.pause_between_requests = pause_between_requests

    # ------------------------------------------------------------------ fetch
    @staticmethod
    def cboe_symbol(symbol: str) -> str:
        return CBOE_SYMBOL_MAP.get(symbol.upper(), symbol.upper())

    def fetch_raw(self, symbol: str) -> dict:
        url = _URL.format(cboe_symbol=self.cboe_symbol(symbol))
        request = urllib.request.Request(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        attempts = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    if resp.status != 200:
                        raise ChainSourceError(
                            f"CBOE returned HTTP {resp.status} for {symbol}"
                        )
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # 429/403 = hard stop. No retry, no reroute.
                if exc.code in (429, 403):
                    raise RateLimitedError(
                        f"CBOE blocked the request for {symbol} "
                        f"(HTTP {exc.code}). Stopping per politeness policy."
                    ) from exc
                attempts += 1
                if attempts > self.max_retries:
                    raise ChainSourceError(
                        f"CBOE HTTP error for {symbol}: {exc}"
                    ) from exc
                time.sleep(2**attempts)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                attempts += 1
                if attempts > self.max_retries:
                    raise ChainSourceError(
                        f"CBOE fetch failed for {symbol}: {exc}"
                    ) from exc
                time.sleep(2**attempts)

    # ------------------------------------------------------------------ parse
    @staticmethod
    def _parse_contract_id(contract_id: str) -> tuple[str, str, float]:
        """Return (expiry YYYY-MM-DD, option_type, strike) from an OCC-style id."""
        match = _CONTRACT_RE.search(contract_id or "")
        if not match:
            raise ChainSourceError(
                f"Unrecognized CBOE contract id format: {contract_id!r}"
            )
        yymmdd, cp, strike_raw = match.groups()
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        option_type = "call" if cp == "C" else "put"
        strike = int(strike_raw) / 1000.0
        return expiry, option_type, strike

    def parse(self, raw: dict, symbol: str) -> pd.DataFrame:
        try:
            data = raw["data"]
            options = data["options"]
            spot = float(data["current_price"])
            quote_local = datetime.strptime(raw["timestamp"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainSourceError(f"CBOE payload shape not recognized: {exc}") from exc

        quote_time_utc = quote_local.replace(tzinfo=CBOE_TZ).astimezone(
            ZoneInfo("UTC")
        ).isoformat()

        rows = []
        for opt in options:
            try:
                expiry, option_type, strike = self._parse_contract_id(
                    opt.get("option", "")
                )
            except ChainSourceError:
                continue  # skip rows we cannot interpret rather than failing all
            # CBOE per-contract "iv" is quoted in DECIMAL (e.g. 0.185 = 18.5%),
            # verified against the live endpoint 2026-09-20: ATM SPY contracts
            # carried iv 0.1012-0.1037 while the same payload's iv30 read
            # 11.675 (percent). NOTE: the top-level "iv30" field IS in
            # percent — only the per-contract "iv" is decimal. Stored as-is
            # per the contract schema ("decimal (0.185 = 18.5%)").
            iv_raw = opt.get("iv")
            iv = float(iv_raw) if iv_raw not in (None, "") else float("nan")
            oi_raw = opt.get("open_interest")
            rows.append(
                {
                    "symbol": symbol,
                    "quote_time": quote_time_utc,
                    "expiry": expiry,
                    "strike": strike,
                    "option_type": option_type,
                    "bid": _f(opt.get("bid")),
                    "ask": _f(opt.get("ask")),
                    "last": _f(opt.get("last_trade_price")),
                    "implied_volatility": iv,
                    "open_interest": int(oi_raw) if oi_raw not in (None, "") else 0,
                    "volume": _i(opt.get("volume")),
                    "delta": _f(opt.get("delta")),
                    "gamma": _f(opt.get("gamma")),
                    "theta": _f(opt.get("theta")),
                    "vega": _f(opt.get("vega")),
                    "spot": spot,
                }
            )

        if not rows:
            raise ChainSourceError(f"CBOE returned no parseable contracts for {symbol}")

        df = pd.DataFrame(rows)
        # Contract schema: volume is nullable int; keep NaN-able.
        df["volume"] = df["volume"].astype("Int64")
        df["open_interest"] = df["open_interest"].astype("Int64")
        return df


def _f(value) -> float:
    return float(value) if value not in (None, "") else float("nan")


def _i(value):
    return int(value) if value not in (None, "") else pd.NA
