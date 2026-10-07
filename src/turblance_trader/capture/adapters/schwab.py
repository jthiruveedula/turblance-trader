"""Schwab Trader API adapter — STUB.

Not yet implemented: it needs a Schwab brokerage account plus a developer
app registration (OAuth), which only Jagadeesh can create.

To activate later (no credentials in the repo — ever):
  1. Open a Schwab brokerage account (if he doesn't already have one).
  2. Register an app at developer.schwab.com to get a client id/secret.
  3. Complete the OAuth consent flow once; refresh tokens are long-lived.
  4. At runtime, supply credentials via environment variables or the
     Secure Vault — never as files or literals in this repo.

Why this is the preferred long-term source: $0 with an account, full chain
(all expiries/strikes) + bid/ask/last + IV + Schwab engine Greeks + OI, and
real-time — all within Schwab's own terms for account holders (no
third-party redistribution, which is fine: this terminal is personal use).
"""

from __future__ import annotations

import pandas as pd

from turblance_trader.capture.adapters.base import ChainAdapter, SetupRequiredError

_SETUP_MESSAGE = (
    "Schwab adapter is not wired up yet. To enable it, Jagadeesh needs to: "
    "(1) hold a Schwab brokerage account, (2) register a developer app at "
    "developer.schwab.com (OAuth client id/secret), and (3) approve supplying "
    "those credentials at runtime via environment variables or the Secure "
    "Vault — never stored in this repo. Say the word and the adapter "
    "implementation lands on top of this stub."
)


class SchwabAdapter(ChainAdapter):
    source_name = "schwab"

    def fetch_raw(self, symbol: str) -> dict:
        raise SetupRequiredError(_SETUP_MESSAGE)

    def parse(self, raw: dict, symbol: str) -> pd.DataFrame:
        raise SetupRequiredError(_SETUP_MESSAGE)
