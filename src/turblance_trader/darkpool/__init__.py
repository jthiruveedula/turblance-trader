"""Off-exchange (dark pool / ATS) data module for turblance-trader.

PERSONAL RESEARCH USE ONLY — FINRA TERMS COMPLIANCE NOTE:
FINRA publishes ATS Transparency data for non-commercial personal or
professional use. This module downloads that data for the user's own
research, keeps it local on this machine (``data/darkpool/``, git-ignored),
and never republishes, re-serves, displays it to third parties, or feeds it
into a public product. If you are not the user this was built for, do not
re-use these downloaders against FINRA's endpoints without reading FINRA's
own terms of use first.

What lives here:
  * :mod:`turblance_trader.darkpool.base` — the ``DarkPoolAdapter``
    interface (``fetch_raw`` -> ``parse`` -> ``capture``), mirroring the
    shape of the chain-capture adapters but for weekly ATS aggregates.
  * :mod:`turblance_trader.darkpool.schema` — the documented ATS row
    contract every adapter normalizes into.
  * :mod:`turblance_trader.darkpool.finra_ats` — FINRA ATS Transparency
    weekly downloader (the $0 path; ~2-week delayed, weekly cadence).
  * :mod:`turblance_trader.darkpool.storage` — weekly file layout under
    ``data/darkpool/``.
  * :mod:`turblance_trader.darkpool.analytics` — historical analytics on
    the ATS data. Interpretive outputs are labeled experimental.
  * :mod:`turblance_trader.darkpool.trf_live` — stub for real-time TRF
    prints via the IBKR adapter (wires up when the gateway goes live).

$0-first, always: no subscriptions, no paid APIs, no credentials anywhere
in this package. No real-money routing. No alert-delivery mechanism.
"""

from turblance_trader.darkpool.base import (
    ATSSourceError,
    CadenceError,
    DarkPoolAdapter,
    RateLimitedError,
    SetupRequiredError,
)
from turblance_trader.darkpool.schema import ATS_SCHEMA_COLUMNS, validate_ats_schema

__all__ = [
    "ATSSourceError",
    "ATS_SCHEMA_COLUMNS",
    "CadenceError",
    "DarkPoolAdapter",
    "RateLimitedError",
    "SetupRequiredError",
    "validate_ats_schema",
]
