"""Alpaca adapter — STUB.

Not yet implemented: it needs an Alpaca account (free tier is enough for the
dev sandbox), which only Jagadeesh can create.

To activate later (no credentials in the repo — ever):
  1. Create a free account at alpaca.markets (paper trading included).
  2. Generate API key + secret from the dashboard.
  3. At runtime, supply them via environment variables or the Secure Vault —
     never as files or literals in this repo.

Why this is a good fallback source: $0, real-time IEX + 15-min delayed SIP
equities, an indicative options feed, and a paper-trading API for the sandbox.
Note Alpaca's market-data terms are personal-use only on the free tier —
fine for this research terminal, and consistent with the no-redistribution
stance documented in the capture README.
"""

from __future__ import annotations

import pandas as pd

from turblance_trader.capture.adapters.base import ChainAdapter, SetupRequiredError

_SETUP_MESSAGE = (
    "Alpaca adapter is not wired up yet. To enable it, Jagadeesh needs to: "
    "(1) create a free account at alpaca.markets, (2) generate an API key + "
    "secret, and (3) approve supplying them at runtime via environment "
    "variables or the Secure Vault — never stored in this repo. Say the "
    "word and the adapter implementation lands on top of this stub."
)


class AlpacaAdapter(ChainAdapter):
    source_name = "alpaca"

    def fetch_raw(self, symbol: str) -> dict:
        raise SetupRequiredError(_SETUP_MESSAGE)

    def parse(self, raw: dict, symbol: str) -> pd.DataFrame:
        raise SetupRequiredError(_SETUP_MESSAGE)
