"""Weekly file storage for ATS data.

Layout:
    data/darkpool/{YYYY-MM-DD}.{csv,parquet}

One file per FINRA report week (the Monday ``week_start`` is the filename).
Format: Parquet when a parquet engine (pyarrow / fastparquet) is importable,
otherwise CSV with identical columns — same convention as
``capture.store``. No parquet engine is installed in this environment
today, so CSV is the active format.

Idempotency: the filename derives from the week itself, so re-running never
duplicates; ``write_weekly`` skips an existing file unless ``force=True``.

``data/`` is git-ignored (see repo .gitignore), so downloaded FINRA data
is local research data only — never committed, never redistributed.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pandas as pd

from turblance_trader.darkpool.schema import ATS_SCHEMA_COLUMNS, validate_ats_schema

_WEEK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


def _parquet_available() -> bool:
    return importlib.util.find_spec("pyarrow") is not None or (
        importlib.util.find_spec("fastparquet") is not None
    )


def _darkpool_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "darkpool"


def weekly_path(week_start: str, data_dir: str | Path) -> Path:
    """Deterministic storage path for a report week."""
    if not _WEEK_RE.match(week_start):
        raise ValueError(f"week_start must be YYYY-MM-DD, got {week_start!r}")
    ext = "parquet" if _parquet_available() else "csv"
    return _darkpool_dir(data_dir) / f"{week_start}.{ext}"


def write_weekly(
    df: pd.DataFrame,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> tuple[Path, bool]:
    """Validate and persist one weekly ATS snapshot.

    Returns (path, written): ``written`` is False when the week's file
    already existed and ``force`` was not given.
    """
    validate_ats_schema(df)
    weeks = df["week_start"].unique()
    if len(weeks) != 1:
        raise ValueError(f"weekly file must cover exactly one week, got {weeks!r}")
    path = weekly_path(str(weeks[0]), data_dir)
    if path.exists() and not force:
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = df[ATS_SCHEMA_COLUMNS]
    if path.suffix == ".parquet":
        ordered.to_parquet(path, index=False)
    else:
        ordered.to_csv(path, index=False)
    return path, True


def read_weekly(path: str | Path) -> pd.DataFrame:
    """Read a weekly file back with stable dtypes for round-trip comparisons."""
    path = Path(path)
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(
            path,
            dtype={
                "week_start": "string",
                "symbol": "string",
                "issue_name": "string",
                "ats_mpid": "string",
                "ats_name": "string",
                "tier": "string",
                "block_bucket": "string",
                "last_updated": "string",
                "source": "string",
            },
        )
    df["weekly_shares"] = pd.to_numeric(df["weekly_shares"], errors="coerce").astype("Int64")
    df["weekly_trades"] = pd.to_numeric(df["weekly_trades"], errors="coerce").astype("Int64")
    return df[ATS_SCHEMA_COLUMNS]


def list_weeks(data_dir: str | Path) -> list[str]:
    """Sorted list of report weeks with local files."""
    d = _darkpool_dir(data_dir)
    if not d.is_dir():
        return []
    weeks = []
    for p in d.iterdir():
        m = _WEEK_RE.match(p.stem)
        if m and p.suffix in (".csv", ".parquet"):
            weeks.append(m.group(1))
    return sorted(weeks)


def newest_download_mtime(data_dir: str | Path) -> float | None:
    """Newest file mtime in data/darkpool/, or None when empty.

    Used by the adapter's weekly-cadence guard.
    """
    d = _darkpool_dir(data_dir)
    if not d.is_dir():
        return None
    mtimes = [
        os.path.getmtime(p)
        for p in d.iterdir()
        if p.is_file() and p.suffix in (".csv", ".parquet")
    ]
    return max(mtimes) if mtimes else None
