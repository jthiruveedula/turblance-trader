"""STUB: real-time TRF (Trade Reporting Facility) prints via IBKR.

Background: real-time off-exchange prints live inside the consolidated SIP
tape; every retail "dark pool API" repackages tape/TRF data. There is no $0
keyless HTTP source for real-time TRF prints, so this adapter is a stub
until Jagadeesh's IBKR gateway goes live.

Future wiring point (all in this file, ``TrfLiveAdapter.fetch_raw``):
  1. IBKR gateway running + market-data subscriptions active
     (see ``src/turblance_trader/capture/README.md`` — "Going live with IBKR").
  2. ``ib_async`` installed in the venv (``pip install ib_async``).
  3. ``TrfLiveAdapter.fetch_raw(symbol)`` subscribes with
     ``ib.reqTickByTickData(contract, tickType="AllLast")`` and collects
     ``TickByTickAllLast`` ticks for a bounded window. Off-exchange prints
     arrive on this feed with the TRF exchange identifier (FINRA/NYSE TRF),
     which is how they are separated from lit prints.
  4. Each tick maps to a print row:
       tick.time            -> print_time (ISO-8601 UTC)
       contract.symbol      -> symbol
       tick.price           -> price
       tick.size            -> shares
       tick.exchange        -> venue ("TRF" when the exchange code is a
                               trade-reporting facility, else the lit venue)
       tick.tickAttribLast  -> sale-condition flags (pastSpecial etc.)
  5. Print rows aggregate into weekly per-symbol totals that feed
     ``analytics.dark_volume_share(..., consolidated=...)`` — that is the
     ``consolidated`` wiring point: weekly consolidated share volume per
     symbol built from these prints.

Print rows do NOT fit the weekly ATS contract schema (they are per-trade,
not per-week-per-venue aggregates), so the live implementation will define
its own ``TRF_PRINT_SCHEMA`` here at wiring time rather than reusing
``darkpool.schema``.

Until then, every method raises :class:`SetupRequiredError` with the exact
steps Jagadeesh needs to take. $0-first: no paid vendor, no new
subscriptions beyond the already-approved IBKR market-data budget.
"""

from __future__ import annotations

import pandas as pd

from turblance_trader.darkpool.base import DarkPoolAdapter, SetupRequiredError

_SETUP_STEPS = """\
Real-time TRF prints are not available yet — the IBKR gateway is not live.
To enable this adapter, Jagadeesh needs to:

  1. Complete the IBKR gateway setup in
     src/turblance_trader/capture/README.md ("Going live with IBKR"):
     market-data subscriptions active (OPRA + US securities bundle, within
     the approved $20/mo budget), Market Data API Acknowledgement accepted,
     IBKR Pro with >= $500 equity, IB Gateway running with socket clients
     enabled (port 4001 live / 4002 paper), Trusted IPs include 127.0.0.1.
  2. Install the client library in the project venv:
       ~/workspace/.venv/bin/pip install ib_async
  3. Verify delayed data first (free, no subscriptions needed):
       IBKR_MARKET_DATA_TYPE=3 python3 scripts/capture_chains.py --once \\
           --source ibkr --symbols SPY
  4. Implement the wiring point documented at the top of
     src/turblance_trader/darkpool/trf_live.py:
     ib_async reqTickByTickData(..., tickType="AllLast") -> filter ticks to
     the TRF exchange identifier -> map each TickByTickAllLast to a print
     row -> define TRF_PRINT_SCHEMA -> aggregate weekly consolidated volume
     per symbol into analytics.dark_volume_share(consolidated=...).

No credentials go in this repo — login happens in the IB Gateway itself.
"""


class TrfLiveAdapter(DarkPoolAdapter):
    """Real-time TRF prints via the IBKR TWS API. Currently a stub."""

    source_name = "trf_live"

    def fetch_raw(self, week_start: str) -> dict:
        """Not implemented until the IBKR gateway is live."""
        raise SetupRequiredError(_SETUP_STEPS)

    def parse(self, raw: dict, week_start: str) -> pd.DataFrame:
        """Not implemented until the IBKR gateway is live."""
        raise SetupRequiredError(_SETUP_STEPS)
