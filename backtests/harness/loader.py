"""Load option-chain snapshots from the forward-capture store.

Capture-store contract (shared with the capture pipeline):
    data/chains/{symbol}/{YYYY-MM-DD}/snapshot_{HHMMSS}_utc.parquet
    (or .csv fallback), one DataFrame per file with columns:
    symbol, quote_time (ISO-8601 UTC), expiry (YYYY-MM-DD), strike (float),
    option_type ('call'|'put'), bid, ask, last, implied_volatility,
    open_interest (int), volume, delta, gamma, theta, vega, spot (float).

This loader returns snapshots as a time-ordered list of
(quote_time, DataFrame) tuples. It tolerates missing days and missing
symbols only in the sense that it raises clear, actionable errors —
it never silently fabricates data.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# Columns every snapshot file must carry (capture-store contract).
REQUIRED_COLUMNS = [
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


class DataStoreError(Exception):
    """Raised when the capture store is missing, empty, or malformed."""


def repo_root() -> Path:
    """Repository root (this file lives at <root>/backtests/harness/loader.py)."""
    return Path(__file__).resolve().parent.parent.parent


def default_store_root() -> Path:
    """Default capture-store root: <repo>/data/chains."""
    return repo_root() / "data" / "chains"


def list_symbols(store_root: Optional[os.PathLike] = None) -> List[str]:
    """Return the symbols present in the capture store, sorted."""
    root = Path(store_root) if store_root is not None else default_store_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def list_days(
    symbol: str, store_root: Optional[os.PathLike] = None
) -> List[str]:
    """Return the YYYY-MM-DD capture days available for a symbol, sorted.

    Raises DataStoreError if the symbol directory does not exist.
    """
    root = Path(store_root) if store_root is not None else default_store_root()
    sym_dir = root / symbol.upper()
    if not sym_dir.is_dir():
        raise DataStoreError(
            f"No capture data for symbol '{symbol.upper()}' under {root}. "
            "The forward-capture pipeline has not written any snapshots yet."
        )
    return sorted(p.name for p in sym_dir.iterdir() if p.is_dir())


def _find_snapshot_files(day_dir: Path) -> List[Path]:
    """Snapshot files for one day, preferring parquet over the csv fallback."""
    parquets = sorted(day_dir.glob("snapshot_*_utc.parquet"))
    if parquets:
        return parquets
    return sorted(day_dir.glob("snapshot_*_utc.csv"))


def _quote_time_of(df: pd.DataFrame, path: Path) -> datetime:
    """Canonical quote_time for one snapshot file.

    Prefer the data's own quote_time column (contract requires it);
    fall back to the filename timestamp only when the column is missing.
    """
    if "quote_time" in df.columns and len(df):
        ts = pd.to_datetime(df["quote_time"], utc=True).iloc[0]
        return ts.to_pydatetime()
    # Fallback: parse snapshot_{HHMMSS}_utc from the filename and the
    # YYYY-MM-DD day from the parent directory.
    day = pd.Timestamp(day_dir_name(path), tz="UTC")
    hhmmss = path.stem.split("_")[1]
    naive = datetime.strptime(hhmmss, "%H%M%S")
    return day.replace(hour=naive.hour, minute=naive.minute, second=naive.second).to_pydatetime()


def day_dir_name(path: Path) -> str:
    return path.parent.name


def _read_snapshot(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataStoreError(
            f"Snapshot {path} is missing required columns: {missing}. "
            "Expected the capture-store contract columns."
        )
    if len(df) == 0:
        raise DataStoreError(f"Snapshot {path} is empty.")
    # Normalize quote_time to UTC datetimes once, at load.
    df = df.copy()
    df["quote_time"] = pd.to_datetime(df["quote_time"], utc=True, errors="coerce")
    if df["quote_time"].isna().any():
        raise DataStoreError(
            f"Snapshot {path} has unparseable quote_time values."
        )
    return df


def load_snapshots(
    symbol: str,
    store_root: Optional[os.PathLike] = None,
    days: Optional[List[str]] = None,
) -> List[Tuple[datetime, pd.DataFrame]]:
    """Load snapshots for a symbol as a time-ordered [(quote_time, df), ...] list.

    Parameters
    ----------
    symbol: underlying symbol, e.g. 'SPY'.
    store_root: capture-store root; defaults to <repo>/data/chains.
    days: optional subset of YYYY-MM-DD day directories to load.

    Raises
    ------
    DataStoreError
        Missing symbol directory, no capture days, no snapshot files, or a
        malformed snapshot file. Never returns silently-incomplete data.
    """
    root = Path(store_root) if store_root is not None else default_store_root()
    sym = symbol.upper()
    sym_dir = root / sym
    if not root.is_dir():
        raise DataStoreError(
            f"Capture store not found at {root}. "
            "The forward-capture pipeline has not been run yet."
        )
    if not sym_dir.is_dir():
        raise DataStoreError(
            f"No capture data for symbol '{sym}' under {root}. "
            f"Known symbols: {list_symbols(root) or 'none'}."
        )
    available = [p for p in sym_dir.iterdir() if p.is_dir()]
    if days is not None:
        wanted = set(days)
        available = [p for p in available if p.name in wanted]
        missing_days = wanted - {p.name for p in available}
        if missing_days:
            raise DataStoreError(
                f"Capture days not found for '{sym}': {sorted(missing_days)}. "
                f"Available: {list_days(sym, root)}."
            )
    if not available:
        raise DataStoreError(
            f"No capture days found for '{sym}' under {sym_dir}."
        )

    snapshots: List[Tuple[datetime, pd.DataFrame]] = []
    for day_dir in sorted(available):
        files = _find_snapshot_files(day_dir)
        if not files:
            raise DataStoreError(
                f"No snapshot files in {day_dir} (expected "
                "snapshot_{HHMMSS}_utc.parquet or .csv)."
            )
        for path in files:
            df = _read_snapshot(path)
            snapshots.append((_quote_time_of(df, path), df))

    # Time-order the full sequence; guard against duplicate quote_times.
    snapshots.sort(key=lambda item: item[0])
    seen = set()
    dupes = set()
    for t, _ in snapshots:
        key = t.isoformat()
        if key in seen:
            dupes.add(key)
        seen.add(key)
    if dupes:
        raise DataStoreError(
            f"Duplicate quote_time snapshots for '{sym}': {sorted(dupes)[:5]}. "
            "Refusing to guess ordering — inspect the capture store."
        )
    return snapshots


def snapshot_days_covered(symbol: str, store_root: Optional[os.PathLike] = None) -> int:
    """Number of capture days with at least one snapshot for a symbol."""
    try:
        return len(list_days(symbol, store_root))
    except DataStoreError:
        return 0
