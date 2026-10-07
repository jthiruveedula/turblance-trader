"""Adapter interface for off-exchange (dark pool / ATS) data sources.

Mirrors the shape of ``turblance_trader.capture.adapters.base``
(``fetch_raw`` -> ``parse`` -> ``capture``) but adapted for ATS data, which
is a weekly aggregate per security per venue rather than a point-in-time
quote snapshot. No credentials are ever stored in this repo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from turblance_trader.darkpool.schema import validate_ats_schema


class ATSSourceError(Exception):
    """Base error for anything that goes wrong talking to an ATS data source."""


class RateLimitedError(ATSSourceError):
    """The source answered HTTP 429/403 (or equivalent).

    HARD STOP: do not retry, do not reroute, do not increase concurrency.
    The runner treats this as a fatal, non-retryable exit.
    """


class SetupRequiredError(ATSSourceError):
    """The adapter needs a setup step Jagadeesh has not completed yet."""


class CadenceError(ATSSourceError):
    """The source was pulled more often than its allowed cadence.

    The FINRA ATS data is weekly and ~2 weeks delayed; pulling it more than
    once a week adds load for zero new information.
    """


class DarkPoolAdapter(ABC):
    """Interface every dark-pool / ATS source adapter must implement."""

    #: Short name used on the CLI, e.g. ``finra_ats``.
    source_name: str = "base"

    @abstractmethod
    def fetch_raw(self, week_start: str) -> dict:
        """Fetch the raw ATS payload for the report week starting ``week_start``.

        ``week_start`` is ``YYYY-MM-DD`` (the Monday partition key FINRA
        publishes under).

        Raises:
            RateLimitedError: source is throttling/blocking us — hard stop.
            ATSSourceError: any other fetch failure (timeout, bad payload...).
        """

    @abstractmethod
    def parse(self, raw: dict, week_start: str) -> pd.DataFrame:
        """Normalize a raw payload into the ATS contract schema.

        Must return exactly the columns in
        :data:`turblance_trader.darkpool.schema.ATS_SCHEMA_COLUMNS`, in order.
        """

    def capture(self, week_start: str) -> pd.DataFrame:
        """Fetch + parse + validate one weekly ATS snapshot."""
        raw = self.fetch_raw(week_start)
        df = self.parse(raw, week_start)
        validate_ats_schema(df)
        return df
