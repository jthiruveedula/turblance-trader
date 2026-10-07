"""Flow-relevant granularity derived from captured chain snapshots.

This module adds the *flow* layer on top of the chain-capture pipeline
without polling any source again:

* **Flow deltas** (``derive_deltas`` / ``derive_day_deltas``): diffs
  consecutive chain snapshots per contract into per-contract
  volume/OI deltas. Stored under
  ``data/flow/deltas/{symbol}/{date}/``. This is a pure function of the
  snapshots the chain cron already banks — zero additional network I/O,
  zero extra CBOE requests.

* **Tick prints** (``write_tick_prints``): storage + schema for the
  IBKR tick-by-tick path (``IBKRAdapter.fetch_ticks``). Stored under
  ``data/flow/ticks/{symbol}/{date}/``.

Both writers are idempotent (filenames derive from the data itself), and
delta derivation never fails a chain-capture run: callers should wrap it
in try/except and log, since the chain snapshot is the primary artifact.

Nothing in this module touches the network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

from turblance_trader.capture.schema import validate_schema
from turblance_trader.capture.store import read_snapshot

# ----------------------------------------------------------------------------
# Flow-delta schema
# ----------------------------------------------------------------------------

DELTA_COLUMNS: list[str] = [
    "symbol",
    "prev_quote_time",
    "curr_quote_time",
    "expiry",
    "strike",
    "option_type",
    "volume_prev",
    "volume_curr",
    "volume_delta",
    "open_interest_prev",
    "open_interest_curr",
    "open_interest_delta",
    "status",
    "flag",
]

_STATUSES = {"ok", "new", "expired"}

# Corporate-action-ish jump detection. A contract's open interest does not
# organically multiply 5x+ (with at least JUMP_MIN_OI_ABS new contracts) in
# one snapshot interval — that pattern means a re-listing, adjustment, or a
# bad print upstream, and must be *flagged*, never silently absorbed as flow.
_JUMP_RATIO = 5.0
_JUMP_MIN_OI_ABS = 1000

# volume is session volume: it is monotonic non-decreasing inside one
# session. A drop means a session boundary, a re-listing, or a bad print.
_FLAGS = {"", "oi_jump", "volume_reset"}


class DeltaSchemaError(ValueError):
    """A flow-delta DataFrame violates the delta contract schema."""


def validate_delta_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the flow-delta contract. Returns ``df``."""
    problems: list[str] = []
    if list(df.columns) != DELTA_COLUMNS:
        problems.append(
            f"columns must be exactly {DELTA_COLUMNS}; got {list(df.columns)}"
        )
    if df.empty:
        problems.append("delta frame is empty")
    for col in ("volume_prev", "volume_curr", "volume_delta",
                "open_interest_prev", "open_interest_curr",
                "open_interest_delta"):
        if col in df.columns:
            vals = df[col].dropna()
            if not pd.api.types.is_numeric_dtype(df[col]):
                problems.append(f"column {col!r} must be numeric")
            elif len(vals) and not ((vals % 1) == 0).all():
                problems.append(f"column {col!r} must hold whole numbers")
    if "status" in df.columns:
        bad = set(df["status"].dropna().unique()) - _STATUSES
        if bad:
            problems.append(f"status has unexpected values: {sorted(bad)}")
    if "flag" in df.columns:
        for value in df["flag"].dropna().unique():
            parts = str(value).split(";") if str(value) else [""]
            bad = set(parts) - _FLAGS
            if bad:
                problems.append(f"flag has unexpected values: {sorted(bad)}")
    for col in ("prev_quote_time", "curr_quote_time"):
        if col in df.columns:
            try:
                pd.to_datetime(df[col], utc=True)
            except Exception as exc:  # noqa: BLE001 - surface as schema error
                problems.append(f"{col} not parseable as UTC ISO-8601: {exc}")
    if problems:
        raise DeltaSchemaError(
            "flow-delta schema violations:\n- " + "\n- ".join(problems)
        )
    return df


# ----------------------------------------------------------------------------
# Delta derivation (pure function of two snapshots)
# ----------------------------------------------------------------------------

_CONTRACT_KEY = ["expiry", "strike", "option_type"]


def _flag_row(row: pd.Series) -> str:
    """Flag corporate-action-ish jumps / session resets, never absorb them."""
    flags: list[str] = []
    oi_prev = row["open_interest_prev"]
    oi_curr = row["open_interest_curr"]
    if pd.notna(oi_prev) and pd.notna(oi_curr) and oi_prev >= 1:
        oi_gain = oi_curr - oi_prev
        if oi_gain >= _JUMP_MIN_OI_ABS and oi_gain >= _JUMP_RATIO * oi_prev:
            flags.append("oi_jump")
    vol_prev = row["volume_prev"]
    vol_curr = row["volume_curr"]
    if pd.notna(vol_prev) and pd.notna(vol_curr) and vol_curr < vol_prev:
        flags.append("volume_reset")
    return ";".join(flags)


def derive_deltas(
    prev: pd.DataFrame, curr: pd.DataFrame
) -> pd.DataFrame:
    """Diff two consecutive chain snapshots into per-contract flow deltas.

    Both frames must satisfy the chain contract schema (``validate_schema``).
    Contracts are matched on (expiry, strike, option_type):

    * present in both -> status ``ok``, volume/OI deltas computed
    * only in curr     -> status ``new``
    * only in prev     -> status ``expired``

    Corporate-action-ish patterns (OI spiking 5x+ with >=1000 new contracts,
    session volume *decreasing*) are flagged in ``flag`` instead of being
    silently absorbed — downstream flow consumers must decide what they mean.

    Pure function: no I/O, no network.
    """
    validate_schema(prev)
    validate_schema(curr)
    prev_qt = str(prev["quote_time"].iloc[0])
    curr_qt = str(curr["quote_time"].iloc[0])
    symbol = str(curr["symbol"].iloc[0])

    keep = _CONTRACT_KEY + ["volume", "open_interest"]
    merged = curr[keep].merge(
        prev[keep],
        on=_CONTRACT_KEY,
        how="outer",
        suffixes=("_curr", "_prev"),
        indicator=True,
    )

    def status_of(how: str) -> str:
        return {"both": "ok", "left_only": "new", "right_only": "expired"}[how]

    rows = []
    for _, r in merged.iterrows():
        st = status_of(r["_merge"])
        vol_prev = r["volume_prev"]
        vol_curr = r["volume_curr"]
        oi_prev = r["open_interest_prev"]
        oi_curr = r["open_interest_curr"]
        if st == "ok":
            vol_delta = (
                (vol_curr - vol_prev)
                if pd.notna(vol_prev) and pd.notna(vol_curr)
                else pd.NA
            )
            oi_delta = oi_curr - oi_prev
        elif st == "new":
            vol_delta = pd.NA
            oi_delta = oi_curr if pd.notna(oi_curr) else pd.NA
        else:  # expired
            vol_delta = pd.NA
            oi_delta = -oi_prev if pd.notna(oi_prev) else pd.NA
        rows.append(
            {
                "symbol": symbol,
                "prev_quote_time": prev_qt,
                "curr_quote_time": curr_qt,
                "expiry": r["expiry"],
                "strike": float(r["strike"]),
                "option_type": r["option_type"],
                "volume_prev": vol_prev,
                "volume_curr": vol_curr,
                "volume_delta": vol_delta,
                "open_interest_prev": oi_prev,
                "open_interest_curr": oi_curr,
                "open_interest_delta": oi_delta,
                "status": st,
                "flag": "",  # filled below
            }
        )

    df = pd.DataFrame(rows, columns=DELTA_COLUMNS)
    for col in ("volume_prev", "volume_curr", "volume_delta",
                "open_interest_prev", "open_interest_curr",
                "open_interest_delta"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["flag"] = df.apply(_flag_row, axis=1)
    return validate_delta_schema(df)


# ----------------------------------------------------------------------------
# Delta storage: data/flow/deltas/{symbol}/{date}/deltas_{prev}_to_{curr}_utc.*
# ----------------------------------------------------------------------------

def _parquet_available() -> bool:
    return importlib.util.find_spec("pyarrow") is not None or (
        importlib.util.find_spec("fastparquet") is not None
    )


def delta_path(
    symbol: str,
    prev_quote_time: str,
    curr_quote_time: str,
    data_dir: str | Path,
) -> Path:
    """Deterministic delta path for one snapshot pair (idempotent writes)."""
    prev_ts = pd.Timestamp(prev_quote_time, tz="UTC")
    curr_ts = pd.Timestamp(curr_quote_time, tz="UTC")
    ext = "parquet" if _parquet_available() else "csv"
    return (
        Path(data_dir)
        / "flow"
        / "deltas"
        / symbol
        / curr_ts.strftime("%Y-%m-%d")
        / f"deltas_{prev_ts.strftime('%H%M%S')}_to_"
        f"{curr_ts.strftime('%H%M%S')}_utc.{ext}"
    )


def write_deltas(
    df: pd.DataFrame,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> tuple[Path, bool]:
    """Validate and persist a delta frame. Returns (path, written)."""
    validate_delta_schema(df)
    path = delta_path(
        str(df["symbol"].iloc[0]),
        str(df["prev_quote_time"].iloc[0]),
        str(df["curr_quote_time"].iloc[0]),
        data_dir,
    )
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = df[DELTA_COLUMNS]
    if path.suffix == ".parquet":
        ordered.to_parquet(path, index=False)
    else:
        ordered.to_csv(path, index=False)
    return path, True


def _chain_snapshot_files(symbol: str, date: str, data_dir: str | Path) -> list[Path]:
    day_dir = Path(data_dir) / "chains" / symbol / date
    if not day_dir.is_dir():
        return []
    return sorted(
        p for p in day_dir.iterdir()
        if p.is_file() and p.name.startswith("snapshot_")
        and p.suffix in (".csv", ".parquet")
    )


def derive_day_deltas(
    symbol: str,
    date: str,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> list[tuple[Path, bool]]:
    """Derive deltas for every consecutive snapshot pair of a symbol-day.

    Reads the snapshots the chain cron banked under
    ``data/chains/{symbol}/{date}/`` and writes one delta file per
    consecutive pair under ``data/flow/deltas/{symbol}/{date}/``.
    Idempotent: already-derived pairs are skipped unless ``force``.

    Returns a list of (path, written). Never touches the network.
    """
    files = _chain_snapshot_files(symbol, date, data_dir)
    if len(files) < 2:
        return []
    snaps = [read_snapshot(f) for f in files]
    out: list[tuple[Path, bool]] = []
    for prev_df, curr_df in zip(snaps, snaps[1:]):
        deltas = derive_deltas(prev_df, curr_df)
        out.append(write_deltas(deltas, data_dir, force=force))
    return out


# ----------------------------------------------------------------------------
# Tick prints (IBKR tick-by-tick path)
# ----------------------------------------------------------------------------

TICK_COLUMNS: list[str] = [
    "symbol",
    "capture_time",   # ISO-8601 UTC when this capture batch started
    "expiry",
    "strike",
    "option_type",
    "tick_time",      # ISO-8601 UTC of the individual print
    "price",
    "size",
    "tick_type",      # "Last" | "AllLast" | "BidAsk" | "MidPoint"
]

_TICK_TYPES = {"Last", "AllLast", "BidAsk", "MidPoint"}


class TickSchemaError(ValueError):
    """A tick-print DataFrame violates the tick contract schema."""


def validate_tick_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Validate ``df`` against the tick-print contract. Returns ``df``."""
    problems: list[str] = []
    if list(df.columns) != TICK_COLUMNS:
        problems.append(
            f"columns must be exactly {TICK_COLUMNS}; got {list(df.columns)}"
        )
    if df.empty:
        problems.append("tick frame is empty")
    for col in ("strike", "price"):
        if col in df.columns and not pd.api.types.is_numeric_dtype(df[col]):
            problems.append(f"column {col!r} must be numeric")
    if "size" in df.columns:
        vals = df["size"].dropna()
        if not pd.api.types.is_numeric_dtype(df["size"]):
            problems.append("column 'size' must be numeric")
        elif len(vals) and not ((vals % 1) == 0).all():
            problems.append("column 'size' must hold whole numbers")
    if "tick_type" in df.columns:
        bad = set(df["tick_type"].dropna().unique()) - _TICK_TYPES
        if bad:
            problems.append(f"tick_type has unexpected values: {sorted(bad)}")
    for col in ("capture_time", "tick_time"):
        if col in df.columns:
            try:
                pd.to_datetime(df[col], utc=True)
            except Exception as exc:  # noqa: BLE001 - surface as schema error
                problems.append(f"{col} not parseable as UTC ISO-8601: {exc}")
    if "option_type" in df.columns:
        bad = set(df["option_type"].dropna().unique()) - {"call", "put"}
        if bad:
            problems.append(f"option_type has unexpected values: {sorted(bad)}")
    if problems:
        raise TickSchemaError(
            "tick schema violations:\n- " + "\n- ".join(problems)
        )
    return df


def tick_path(symbol: str, capture_time: str, data_dir: str | Path) -> Path:
    """Deterministic tick-print path for one capture batch."""
    ts = pd.Timestamp(capture_time, tz="UTC")
    ext = "parquet" if _parquet_available() else "csv"
    return (
        Path(data_dir)
        / "flow"
        / "ticks"
        / symbol
        / ts.strftime("%Y-%m-%d")
        / f"ticks_{ts.strftime('%H%M%S')}_utc.{ext}"
    )


def write_tick_prints(
    df: pd.DataFrame,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> tuple[Path, bool]:
    """Validate and persist one batch of tick prints. Returns (path, written)."""
    validate_tick_schema(df)
    path = tick_path(
        str(df["symbol"].iloc[0]), str(df["capture_time"].iloc[0]), data_dir
    )
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = df[TICK_COLUMNS]
    if path.suffix == ".parquet":
        ordered.to_parquet(path, index=False)
    else:
        ordered.to_csv(path, index=False)
    return path, True
