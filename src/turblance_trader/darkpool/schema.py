"""Contract schema for ATS (dark pool) rows.

This is the column contract every dark-pool adapter normalizes into and
every downstream module codes against. Do not add, remove, or reorder
columns without updating all consumers.

The schema is deliberately separate from the options-chain contract
(``capture.schema``): ATS data is a weekly aggregate per security per
venue, not a point-in-time quote snapshot.

Column semantics:
  week_start    YYYY-MM-DD, first business day (Monday) of the FINRA report
                week; the partition key for storage filenames.
  symbol        issue symbol identifier, e.g. "SPY".
  issue_name    company/issue name as published by FINRA (nullable).
  ats_mpid      four-character ATS venue identifier, e.g. "AQUA".
  ats_name      ATS company name (nullable).
  tier          "T1" | "T2" | "OTCE" (FINRA's NMS-tier reporting buckets).
  weekly_shares aggregate weekly shares for this symbol at this ATS (>= 0).
  weekly_trades aggregate weekly trade count for this symbol at this ATS (>= 0).
  block_bucket  block-size bucket code for ATS-Blocks summary rows only;
                one of "2K", "10K", "200K", "10K-200K", "100K", "2K-100K"
                (FINRA's bucket codes). Null for ordinary weekly ATS rows.
  last_updated  lastUpdateDate from FINRA, YYYY-MM-DD (nullable).
  source        provenance tag, e.g. "finra_ats".

FINRA bucket codes (ATS Blocks summaries, per the FINRA v0.4 spec):
  "2K"      2K to <10K shares
  "10K"     10K+ shares
  "200K"    $200K+
  "10K-200K" 10K+ shares AND $200K+
  "100K"    $100K to <$200K
  "2K-100K" 2K to <10K shares AND $100K to <$200K
"""

from __future__ import annotations

import pandas as pd

ATS_SCHEMA_COLUMNS: list[str] = [
    "week_start",
    "symbol",
    "issue_name",
    "ats_mpid",
    "ats_name",
    "tier",
    "weekly_shares",
    "weekly_trades",
    "block_bucket",
    "last_updated",
    "source",
]

#: FINRA ATS-Blocks bucket codes (v0.4 spec, summaryTypeCode enumerations).
BLOCK_BUCKETS = frozenset(
    {"2K", "10K", "200K", "10K-200K", "100K", "2K-100K"}
)

#: FINRA NMS-tier reporting buckets.
TIERS = frozenset({"T1", "T2", "OTCE"})

_INT_COLS = ["weekly_shares", "weekly_trades"]


class SchemaError(ValueError):
    """An ATS DataFrame violates the contract schema."""


def _is_nullish(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype("string") == "")


def validate_ats_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the ATS contract schema. Returns ``df`` unchanged.

    Raises:
        SchemaError: with a human-readable list of every violation found.
    """
    problems: list[str] = []

    if list(df.columns) != ATS_SCHEMA_COLUMNS:
        problems.append(
            f"columns must be exactly {ATS_SCHEMA_COLUMNS}; got {list(df.columns)}"
        )
    if df.empty:
        problems.append("ATS snapshot is empty")

    for col in _INT_COLS:
        if col in df.columns:
            if not pd.api.types.is_numeric_dtype(df[col]):
                problems.append(f"column {col!r} must be numeric")
            else:
                vals = df[col].dropna()
                if len(vals) and not ((vals % 1) == 0).all():
                    problems.append(f"column {col!r} must hold whole numbers")
                if len(vals) and (vals < 0).any():
                    problems.append(f"column {col!r} must be >= 0")

    if "tier" in df.columns:
        bad = set(df["tier"].dropna().unique()) - TIERS
        if bad:
            problems.append(f"tier has unexpected values: {sorted(bad)}")

    if "week_start" in df.columns:
        try:
            pd.to_datetime(df["week_start"], format="%Y-%m-%d")
        except Exception as exc:  # noqa: BLE001 - surface as schema error
            problems.append(f"week_start not in YYYY-MM-DD form: {exc}")

    if "block_bucket" in df.columns:
        present = df["block_bucket"][~_is_nullish(df["block_bucket"])]
        bad = set(present.unique()) - BLOCK_BUCKETS
        if bad:
            problems.append(f"block_bucket has unexpected values: {sorted(bad)}")

    if "last_updated" in df.columns:
        present = df["last_updated"][~_is_nullish(df["last_updated"])]
        if len(present):
            try:
                pd.to_datetime(present, format="%Y-%m-%d")
            except Exception as exc:  # noqa: BLE001 - surface as schema error
                problems.append(f"last_updated not in YYYY-MM-DD form: {exc}")

    for col in ("symbol", "ats_mpid"):
        if col in df.columns:
            if _is_nullish(df[col]).any():
                problems.append(f"column {col!r} must be present on every row")

    if problems:
        raise SchemaError("ATS schema violations:\n- " + "\n- ".join(problems))
    return df
