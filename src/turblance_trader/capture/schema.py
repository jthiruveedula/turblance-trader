"""Shared contract schema for options-chain snapshots.

This is the column contract every capture adapter normalizes into and every
downstream module (GEX engine, flow scanner, backtests) codes against.
Do not add, remove, or reorder columns without updating all consumers.

Column semantics:
  symbol             underlying ticker, e.g. "SPY" (never the CBOE "_SPX" spelling)
  quote_time         ISO-8601 UTC timestamp string of the underlying quote,
                     e.g. "2026-09-18T19:59:00+00:00"
  expiry             contract expiry as "YYYY-MM-DD"
  strike             strike price, float
  option_type        "call" or "put" only
  bid / ask          best bid/ask at quote_time, float (NaN if unavailable)
  last               last trade price, float (NaN ok)
  implied_volatility decimal IV, e.g. 0.185 = 18.5% (NaN ok)
  open_interest      integer contracts outstanding (0 when unknown)
  volume             integer contracts traded in session (nullable)
  delta / gamma / theta / vega  source-model Greeks, float (NaN ok)
  spot               underlying price at quote_time, float
"""

from __future__ import annotations

import pandas as pd

SCHEMA_COLUMNS: list[str] = [
    "symbol",
    "quote_time",
    "expiry",
    "strike",
    "option_type",
    "bid",
    "ask",
    "last",
    "implied_volatility",
    "open_interest",
    "volume",
    "delta",
    "gamma",
    "theta",
    "vega",
    "spot",
]

_OPTION_TYPES = {"call", "put"}
_FLOAT_COLS = [
    "strike", "bid", "ask", "last", "implied_volatility",
    "delta", "gamma", "theta", "vega", "spot",
]
_INT_COLS = ["open_interest", "volume"]


class SchemaError(ValueError):
    """A snapshot DataFrame violates the contract schema."""


def validate_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the contract schema. Returns ``df`` unchanged.

    Raises:
        SchemaError: with a human-readable list of every violation found.
    """
    problems: list[str] = []

    if list(df.columns) != SCHEMA_COLUMNS:
        problems.append(
            f"columns must be exactly {SCHEMA_COLUMNS}; got {list(df.columns)}"
        )
    if df.empty:
        problems.append("snapshot is empty")

    for col in _FLOAT_COLS:
        if col in df.columns and not pd.api.types.is_numeric_dtype(df[col]):
            problems.append(f"column {col!r} must be numeric")

    for col in _INT_COLS:
        if col in df.columns:
            vals = df[col].dropna()
            if not pd.api.types.is_numeric_dtype(df[col]):
                problems.append(f"column {col!r} must be numeric")
            elif len(vals) and not ((vals % 1) == 0).all():
                problems.append(f"column {col!r} must hold whole numbers")

    if "option_type" in df.columns:
        bad = set(df["option_type"].dropna().unique()) - _OPTION_TYPES
        if bad:
            problems.append(f"option_type has unexpected values: {sorted(bad)}")

    if "quote_time" in df.columns:
        try:
            pd.to_datetime(df["quote_time"], utc=True)
        except Exception as exc:  # noqa: BLE001 - surface as schema error
            problems.append(f"quote_time not parseable as UTC ISO-8601: {exc}")

    if "expiry" in df.columns:
        try:
            pd.to_datetime(df["expiry"], format="%Y-%m-%d")
        except Exception as exc:  # noqa: BLE001 - surface as schema error
            problems.append(f"expiry not in YYYY-MM-DD form: {exc}")

    if "spot" in df.columns and df["spot"].isna().any():
        problems.append("spot must be present on every row")

    if problems:
        raise SchemaError("snapshot schema violations:\n- " + "\n- ".join(problems))
    return df
