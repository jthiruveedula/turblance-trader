"""Historical analytics on ATS (dark pool) data.

All functions are pure (no I/O, no network) and NaN-safe: missing inputs
propagate as NaN rather than raising, and divisions by zero yield NaN, not
infinities. Inputs are expected to already satisfy the ATS contract schema.

Interpretive outputs (shares of "total", concentration, block-bucket mix)
are EXPERIMENTAL — they describe the FINRA ATS sample, not the full
consolidated tape. Treat them as research signals for backtesting, not as
statements of fact about market-wide dark volume.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Columns every function here needs present on its input.
_REQUIRED = ["week_start", "symbol", "ats_mpid", "weekly_shares", "weekly_trades"]


def _check(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"analytics input missing columns: {missing}")
    return df.copy()


def _safe_div(num: pd.Series, den: pd.Series) -> pd.Series:
    """Element-wise division that yields NaN (not inf) on zero/NaN denominators."""
    num = pd.to_numeric(num, errors="coerce").astype(float)
    den = pd.to_numeric(den, errors="coerce").astype(float)
    out = num / den.replace(0.0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def weekly_symbol_totals(df: pd.DataFrame) -> pd.DataFrame:
    """Per (week, symbol) ATS aggregates across all venues.

    Returns columns: week_start, symbol, ats_shares, ats_trades, n_ats,
    avg_trade_size (shares per trade, NaN when undefined).
    """
    df = _check(df)
    g = df.groupby(["week_start", "symbol"], dropna=False)
    out = g.agg(
        ats_shares=("weekly_shares", "sum"),
        ats_trades=("weekly_trades", "sum"),
        n_ats=("ats_mpid", "nunique"),
    ).reset_index()
    out["avg_trade_size"] = _safe_div(out["ats_shares"], out["ats_trades"])
    return out


def ats_share_within_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """Each row's share of its symbol's total ATS volume that week.

    Adds ``share_of_symbol_shares`` and ``share_of_symbol_trades``
    (0..1, NaN when the symbol's weekly total is 0/NaN). Sums to ~1.0 per
    (week, symbol) by construction. EXPERIMENTAL: share *within the ATS
    sample*, not of consolidated volume.
    """
    df = _check(df)
    totals = df.groupby(["week_start", "symbol"])[["weekly_shares", "weekly_trades"]].transform("sum")
    out = df.copy()
    out["share_of_symbol_shares"] = _safe_div(df["weekly_shares"], totals["weekly_shares"])
    out["share_of_symbol_trades"] = _safe_div(df["weekly_trades"], totals["weekly_trades"])
    return out


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    """ATS concentration per (week, symbol). EXPERIMENTAL.

    * ``hhi`` — Herfindahl index over venues' share of weekly_shares
      (1.0 = single venue, -> 1/n for n equal venues).
    * ``top1_share`` / ``top3_share`` — largest 1 / 3 venues' share of
      weekly_shares.
    """
    df = _check(df)
    shares = ats_share_within_symbol(df)[["week_start", "symbol", "share_of_symbol_shares"]]
    shares = shares.dropna(subset=["share_of_symbol_shares"])
    g = shares.groupby(["week_start", "symbol"])["share_of_symbol_shares"]
    hhi = g.apply(lambda s: float((s**2).sum()))
    top1 = g.apply(lambda s: float(s.nlargest(1).sum()))
    top3 = g.apply(lambda s: float(s.nlargest(3).sum()))
    out = pd.DataFrame(
        {"hhi": hhi, "top1_share": top1, "top3_share": top3}
    ).reset_index()
    return out


def ats_market_share(df: pd.DataFrame) -> pd.DataFrame:
    """Each ATS venue's share of total ATS volume that week. EXPERIMENTAL.

    Returns per (week_start, ats_mpid): share_of_ats_shares,
    share_of_ats_trades. Describes venue market share *within the FINRA ATS
    sample only*.
    """
    df = _check(df)
    g = df.groupby(["week_start", "ats_mpid"], dropna=False)
    venue = g.agg(shares=("weekly_shares", "sum"), trades=("weekly_trades", "sum")).reset_index()
    totals = venue.groupby("week_start")[["shares", "trades"]].transform("sum")
    venue["share_of_ats_shares"] = _safe_div(venue["shares"], totals["shares"])
    venue["share_of_ats_trades"] = _safe_div(venue["trades"], totals["trades"])
    return venue.drop(columns=["shares", "trades"])


def week_over_week(df: pd.DataFrame) -> pd.DataFrame:
    """Week-over-week change per (symbol, ats_mpid). EXPERIMENTAL.

    Returns one row per (week_start, symbol, ats_mpid) with
    ``shares_wow`` / ``trades_wow`` (fractional change vs the prior week
    present in the data, NaN when there is no prior week or the prior
    week's value was 0) and the absolute deltas ``shares_delta`` /
    ``trades_delta``.
    """
    df = _check(df)
    key = ["symbol", "ats_mpid"]
    w = df.copy()
    w["_w"] = pd.to_datetime(w["week_start"], format="%Y-%m-%d", errors="coerce")
    w = w.sort_values(key + ["_w"])
    for col, out in (("weekly_shares", "shares_wow"), ("weekly_trades", "trades_wow")):
        prev = w.groupby(key, dropna=False)[col].shift(1)
        cur = pd.to_numeric(w[col], errors="coerce").astype(float)
        prevf = pd.to_numeric(prev, errors="coerce").astype(float)
        w[out] = (cur - prevf) / prevf.replace(0.0, np.nan)
        w[out] = w[out].replace([np.inf, -np.inf], np.nan)
        w[out.replace("_wow", "_delta")] = cur - prevf
    return w.drop(columns=["_w"])[
        ["week_start", "symbol", "ats_mpid",
         "shares_wow", "shares_delta", "trades_wow", "trades_delta"]
    ]


def dark_volume_share(
    df: pd.DataFrame, consolidated: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Per-symbol dark-volume share of total weekly volume. EXPERIMENTAL.

    FINRA's ATS file alone cannot give share of *total* consolidated
    volume — it only covers ATS prints. So this function has two modes:

    * ``consolidated=None`` (default): returns per-symbol ATS shares/trades
      with ``dark_share`` = NaN and a ``note`` explaining the data gap.
      Honest, not invented.
    * ``consolidated`` given as a DataFrame with columns
      (week_start, symbol, total_shares): returns
      ``dark_share = ats_shares / total_shares`` per (week, symbol),
      NaN when the consolidated total is missing or 0.

    The ``consolidated`` input is the future wiring point for the real-time
    TRF path (``trf_live``): weekly consolidated share volume per symbol
    aggregated from SIP/TRF prints via the IBKR adapter.
    """
    df = _check(df)
    totals = weekly_symbol_totals(df)[["week_start", "symbol", "ats_shares", "ats_trades"]]
    if consolidated is None:
        totals["dark_share"] = np.nan
        totals["note"] = (
            "ATS-only sample: share of consolidated volume unknown without "
            "a consolidated-volume input (see trf_live for the future path)"
        )
        return totals
    need = {"week_start", "symbol", "total_shares"}
    if not need.issubset(consolidated.columns):
        raise ValueError(f"consolidated must have columns {sorted(need)}")
    merged = totals.merge(
        consolidated[["week_start", "symbol", "total_shares"]],
        on=["week_start", "symbol"],
        how="left",
    )
    merged["dark_share"] = _safe_div(merged["ats_shares"], merged["total_shares"])
    merged["note"] = np.where(
        merged["total_shares"].isna(),
        "no consolidated volume for this (week, symbol)",
        "",
    )
    return merged


def block_bucket_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ATS-Blocks rows by block-size bucket. EXPERIMENTAL.

    Works on rows where ``block_bucket`` is set (the ATS Blocks summaries;
    ordinary weekly ATS rows carry no bucket and are ignored). Returns per
    (week_start, block_bucket): block_shares, block_trades, share_of_block_shares,
    avg_block_size. Returns an empty frame (with these columns) when the
    input has no bucketed rows — the data simply doesn't support it.
    """
    if "block_bucket" not in df.columns:
        raise ValueError("input needs a 'block_bucket' column")
    cols = ["week_start", "symbol", "ats_mpid", "weekly_shares", "weekly_trades",
            "block_bucket"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"block analytics input missing columns: {missing}")
    b = df[df["block_bucket"].notna() & (df["block_bucket"] != "")].copy()
    out_cols = ["week_start", "block_bucket", "block_shares", "block_trades",
                "share_of_block_shares", "avg_block_size"]
    if b.empty:
        return pd.DataFrame({c: pd.Series(dtype="float64" if c not in
                             ("week_start", "block_bucket") else "object")
                             for c in out_cols})
    g = b.groupby(["week_start", "block_bucket"], dropna=False)
    out = g.agg(
        block_shares=("weekly_shares", "sum"),
        block_trades=("weekly_trades", "sum"),
    ).reset_index()
    week_totals = out.groupby("week_start")["block_shares"].transform("sum")
    out["share_of_block_shares"] = _safe_div(out["block_shares"], week_totals)
    out["avg_block_size"] = _safe_div(out["block_shares"], out["block_trades"])
    return out[out_cols]
