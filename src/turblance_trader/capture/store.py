"""Snapshot storage for options-chain captures.

Layout (per the shared contract):
    data/chains/{symbol}/{YYYY-MM-DD}/snapshot_{HHMMSS}_utc.{csv,parquet}

Format: Parquet when a parquet engine (pyarrow / fastparquet) is importable,
otherwise CSV with identical columns. Both are written without an index, so
the column contract in ``schema.py`` is byte-for-byte the same either way.
No parquet engine is installed in this environment today, so CSV is the
active format. Upgrade path: ``pip install pyarrow`` in a venv — no code
changes needed, ``store.py`` detects the engine automatically.

Idempotency: the filename derives from the snapshot's own ``quote_time``,
so re-running for the same quote does not duplicate data. If the target file
already exists and ``force=False``, the write is skipped and the existing
path is returned.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from turblance_trader.capture.schema import SCHEMA_COLUMNS, validate_schema


def _parquet_available() -> bool:
    return importlib.util.find_spec("pyarrow") is not None or (
        importlib.util.find_spec("fastparquet") is not None
    )


def snapshot_path(symbol: str, quote_time_utc: str, data_dir: str | Path) -> Path:
    """Deterministic snapshot path for a symbol + quote_time (ISO-8601 UTC)."""
    ts = pd.Timestamp(quote_time_utc, tz="UTC")
    ext = "parquet" if _parquet_available() else "csv"
    return (
        Path(data_dir)
        / "chains"
        / symbol
        / ts.strftime("%Y-%m-%d")
        / f"snapshot_{ts.strftime('%H%M%S')}_utc.{ext}"
    )


def write_snapshot(
    df: pd.DataFrame,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> tuple[Path, bool]:
    """Validate and persist a snapshot.

    Returns (path, written): ``written`` is False when an identical snapshot
    already existed and ``force`` was not given.
    """
    validate_schema(df)
    quote_time = str(df["quote_time"].iloc[0])
    symbol = str(df["symbol"].iloc[0])
    path = snapshot_path(symbol, quote_time, data_dir)
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = df[SCHEMA_COLUMNS]
    if path.suffix == ".parquet":
        ordered.to_parquet(path, index=False)
    else:
        ordered.to_csv(path, index=False)
    return path, True


def read_snapshot(path: str | Path) -> pd.DataFrame:
    """Read a snapshot back with stable dtypes for round-trip comparisons."""
    path = Path(path)
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(
            path,
            dtype={
                "symbol": "string",
                "quote_time": "string",
                "expiry": "string",
                "option_type": "string",
                "open_interest": "Int64",
                "volume": "Int64",
            },
        )
    df["open_interest"] = df["open_interest"].astype("Int64")
    df["volume"] = df["volume"].astype("Int64")
    return df[SCHEMA_COLUMNS]
