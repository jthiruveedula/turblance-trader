"""Per-contract gamma exposure computation for the turblance-trader GEX engine.

DEALER POSITIONING CONVENTION (read this first)
-----------------------------------------------
We do not observe whether market makers are long or short each contract, so
the engine applies an explicit, flippable assumption via ``dealer_position``:

- ``dealer_position='short'`` (default): customers are assumed to be the net
  buyers of options and dealers/market makers are assumed to be net SHORT
  those options. Dealer GEX = -1 * customer GEX.
- ``dealer_position='long'``: dealers are assumed net LONG the options.
  Dealer GEX = +1 * customer GEX.
- ``dealer_position='flat'``: no dealer position; all exposure is zero.

The assumption is a parameter, not a hidden constant, so downstream analysis
can (and should) flip it when testing sensitivity. Every downstream quantity
in this package (by_strike, by_expiry, flip, walls, king node, regime) is
*dealer-convention* GEX.

FORMULA
-------
Dollar gamma exposure per contract row, in dollars per 1% move of the
underlying::

    customer_gex = gamma * open_interest * 100 * spot**2 * 0.01
    dealer_gex   = dealer_sign * customer_gex

Gamma is per-$1 move; open_interest * 100 is shares; spot**2 * 0.01 converts a
per-$1 exposure into a per-1%-move dollar exposure. Customer gamma is
non-negative by construction (long-option gamma >= 0), so under the default
'short' convention dealer GEX is non-positive at the row level before
aggregation; cross-strike aggregation still carries sign information through
call/put netting in ``levels.by_strike``.

GAMMA SOURCE
------------
Per-row gamma is taken from the ``gamma`` column when present and finite.
When gamma is NaN the engine falls back to computing it from the
``implied_volatility`` column with the Black-Scholes formula in
``black_scholes.gamma`` (needs time-to-expiry from ``quote_time``/``expiry``
and the ``risk_free_rate`` parameter). Rows missing *both* gamma and implied
volatility carry no usable exposure information: they are skipped, and the
skip is counted in the returned metadata (``skipped_rows``).
"""

import pandas as pd

from .black_scholes import gamma as bs_gamma

_DEALER_SIGNS = {"short": -1.0, "long": 1.0, "flat": 0.0}

REQUIRED_COLUMNS = (
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
)


def _years_to_expiry(quote_time, expiry):
    """Years from quote_time to expiry.

    Convention: T = (expiry_date - quote_date).days / 365, clipped at 0.
    This is the standard day-count approximation used only for the
    gamma-fallback path (rows whose gamma column is NaN); rows with a
    supplied gamma never touch this.
    """
    # errors="coerce": a malformed quote_time/expiry degrades to NaT (and then
    # to a 0 fallback gamma via the NaN-safe BS path) instead of raising.
    quote_dates = pd.to_datetime(quote_time, utc=True, errors="coerce").dt.date
    expiry_dates = pd.to_datetime(expiry, format="%Y-%m-%d", errors="coerce").dt.date
    days = (expiry_dates - quote_dates).apply(
        lambda d: d.days if d is not None and pd.notna(d) else float("nan")
    )
    years = days.astype(float) / 365.0
    return years.clip(lower=0.0)


def compute_row_gamma(df, risk_free_rate=0.0):
    """Resolve a per-row gamma Series for an option chain DataFrame.

    Uses the ``gamma`` column where finite; otherwise computes Black-Scholes
    gamma from ``implied_volatility`` (with T from quote_time/expiry).

    Returns
    -------
    tuple (gamma_series, missing_mask)
        ``gamma_series``: float Series of resolved gammas (0.0 where missing).
        ``missing_mask``: boolean Series, True for rows where gamma was NaN
        *and* implied_volatility was NaN (i.e. rows with no usable gamma).
    """
    supplied = pd.to_numeric(df["gamma"], errors="coerce")
    has_supplied = supplied.notna()

    iv = pd.to_numeric(df["implied_volatility"], errors="coerce")
    has_iv = iv.notna()

    needs_bs = (~has_supplied) & has_iv
    bs_vals = pd.Series(0.0, index=df.index, dtype=float)
    if needs_bs.any():
        tte = _years_to_expiry(df["quote_time"], df["expiry"])
        bs_vals = pd.Series(
            bs_gamma(
                spot=pd.to_numeric(df["spot"], errors="coerce"),
                strike=pd.to_numeric(df["strike"], errors="coerce"),
                time_to_expiry=tte,
                volatility=iv,
                risk_free_rate=risk_free_rate,
            ),
            index=df.index,
            dtype=float,
        ).fillna(0.0)

    missing = (~has_supplied) & (~has_iv)

    resolved = pd.Series(0.0, index=df.index, dtype=float)
    resolved[has_supplied] = supplied[has_supplied]
    resolved[needs_bs] = bs_vals[needs_bs]
    return resolved.fillna(0.0), missing


def compute_exposure(df, risk_free_rate=0.0, dealer_position="short"):
    """Compute per-row dealer gamma exposure ($ per 1% underlying move).

    Parameters
    ----------
    df : pandas.DataFrame
        One row per option contract, with the shared contract columns
        (symbol, quote_time, expiry, strike, option_type, bid, ask, last,
        implied_volatility, open_interest, volume, delta, gamma, theta,
        vega, spot).
    risk_free_rate : float, default 0.0
        Risk-free rate (decimal) used only on the gamma-fallback path.
    dealer_position : {'short', 'long', 'flat'}, default 'short'
        The explicit dealer-positioning assumption; see the module docstring.

    Returns
    -------
    tuple (exposure_df, meta)
        ``exposure_df``: copy of the usable rows with added columns
        ``resolved_gamma`` (gamma actually used) and ``dealer_gex`` (dollar
        exposure per 1% move, dealer convention applied).
        ``meta``: dict with ``skipped_rows`` (rows missing both gamma and
        IV), ``n_rows`` (input rows), and ``dealer_position``.
    """
    if dealer_position not in _DEALER_SIGNS:
        raise ValueError(
            f"dealer_position must be one of {sorted(_DEALER_SIGNS)}, "
            f"got {dealer_position!r}"
        )
    sign = _DEALER_SIGNS[dealer_position]

    resolved_gamma, missing = compute_row_gamma(df, risk_free_rate=risk_free_rate)
    skipped = int(missing.sum())

    usable = df.loc[~missing].copy()
    g = resolved_gamma.loc[~missing]
    oi = pd.to_numeric(usable["open_interest"], errors="coerce").fillna(0).astype(float)
    spot = pd.to_numeric(usable["spot"], errors="coerce").fillna(0.0)

    customer_gex = g * oi * 100.0 * spot**2 * 0.01
    usable["resolved_gamma"] = g.values
    usable["dealer_gex"] = sign * customer_gex.values

    meta = {
        "n_rows": int(len(df)),
        "skipped_rows": skipped,
        "dealer_position": dealer_position,
    }
    return usable, meta
