"""Clean adapter interface for options-chain quote sources.

Every adapter fetches a raw chain payload and normalizes it into the shared
contract schema defined in :mod:`turblance_trader.capture.schema`. Adding a
new source = subclassing :class:`ChainAdapter` and implementing two methods.
No credentials are ever stored in this repo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from turblance_trader.capture.schema import validate_schema


class ChainSourceError(Exception):
    """Base error for anything that goes wrong talking to a quote source."""


class RateLimitedError(ChainSourceError):
    """The source answered HTTP 429/403 (or equivalent).

    HARD STOP: do not retry, do not reroute, do not increase concurrency.
    The runner treats this as a fatal, non-retryable exit.
    """


class SetupRequiredError(ChainSourceError):
    """The adapter needs an account / app registration Jagadeesh has not set up yet."""


class ChainAdapter(ABC):
    """Interface every chain source adapter must implement."""

    #: Short name used on the CLI, e.g. ``cboe``.
    source_name: str = "base"

    @abstractmethod
    def fetch_raw(self, symbol: str) -> dict:
        """Fetch the raw chain payload for ``symbol``.

        Raises:
            RateLimitedError: source is throttling/blocking us — hard stop.
            ChainSourceError: any other fetch failure (timeout, bad payload...).
        """

    @abstractmethod
    def parse(self, raw: dict, symbol: str) -> pd.DataFrame:
        """Normalize a raw payload into the shared contract schema.

        Must return exactly the columns in
        :data:`turblance_trader.capture.schema.SCHEMA_COLUMNS`, in order.
        """

    def capture(self, symbol: str) -> pd.DataFrame:
        """Fetch + parse + validate a single snapshot for ``symbol``."""
        raw = self.fetch_raw(symbol)
        df = self.parse(raw, symbol)
        validate_schema(df)
        return df
