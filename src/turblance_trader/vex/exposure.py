"""Per-contract vega exposure computation for the turblance-trader VEX engine.

DEALER POSITIONING CONVENTION (read this first)
-----------------------------------------------
We do not observe whether market makers are long or short each contract, so
the engine applies an explicit, flippable assumption via ``dealer_position``
(identical to the GEX engine in ``..gex.exposure``):

- ``dealer_position='short'`` (default): customers are assumed to be the net
  buyers of options and dealers/market makers are assumed to be net SHORT
  those options. Dealer VEX = -1 * customer VEX.
- ``dealer_position='long'``: dealers are assumed net LONG the options.
  Dealer VEX = +1 * customer VEX.
- ``dealer_position='flat'``: no dealer position; all exposure is zero.

Every downstream quantity in this package (by_strike, by_expiry, flip,
walls, king node, regime) is *dealer-convention* VEX.

UNITS — THE CONTRACT (read this second)
---------------------------------------
VEX is reported in **dollars per 1 vol point (1%) move of implied
volatility**:

    customer_vex = vega_per_1pt * open_interest * 100
    dealer_vex   = dealer_sign * customer_vex

where ``vega_per_1pt`` is the per-share dollar vega per *1 percentage point*
of IV. Source feeds quote vega per 1.0 (100%) IV move (the textbook
convention, and the convention of our own Black-Scholes fallback), so by
default it is divided by ``VOL_POINT_DIVISOR = 100.0`` to reach the
per-1-point basis. The convention is both a named constant *and* a
parameter (``source_vega_iv_unit``): if your feed quotes vega per 1 vol
point already, pass ``source_vega_iv_unit=0.01`` and no division happens.

Units are never mixed silently: the per-row resolved vega column is kept in
the module's reported unit basis, and both the fallback and supplied paths
pass through the same normalization.

VEGA SOURCE
-----------
Per-row vega is taken from the ``vega`` column when present and finite
(assumed quoted per 1.0 IV move — see above). When vega is NaN the engine
falls back to computing it from the ``implied_volatility`` column with the
Black-Scholes formula in ``..gex.black_scholes.vega`` (needs time-to-expiry
from ``quote_time``/``expiry`` and the ``risk_free_rate`` parameter). Rows
missing *both* vega and implied volatility carry no usable exposure
information: they are skipped, and the skip is counted in the returned
metadata (``skipped_rows``).

Unlike gamma, vega does not need ``spot`` in the formula — only the
normalization divisor and ``open_interest * 100`` (shares per contract).
"""

import pandas as pd

from ..capture.schema import SCHEMA_COLUMNS
from ..gex.black_scholes import vega as bs_vega

_DEALER_SIGNS = {"short": -1.0, "long": 1.0, "flat": 0.0}

# The VEX unit contract: exposure is reported in dollars per 1 vol point
# (1% IV move). A vega quoted per 1.0 (100%) IV move is divided by this to
# reach that basis. See the module docstring.
VOL_POINT_DIVISOR = 100.0

REQUIRED_COLUMNS = tuple(SCHEMA_COLUMNS)  # the shared 16-column chain schema


def _years_to_expiry(quote_time, expiry):
    """Years from quote_time to expiry.

    Convention: T = (expiry_date - quote_date).days / 365, clipped at 0.
    This is the standard day-count approximation used only for the
    vega-fallback path (rows whose vega column is NaN); rows with a
    supplied vega never touch this.
    """
    # errors="coerce": a malformed quote_time/expiry degrades to NaT (and then
    # to a 0 fallback vega via the NaN-safe BS path) instead of raising.
    quote_dates = pd.to_datetime(quote_time, utc=True, errors="coerce").dt.date
    expiry_dates = pd.to_datetime(expiry, format="%Y-%m-%d", errors="coerce").dt.date
    days = (expiry_dates - quote_dates).apply(
        lambda d: d.days if d is not None and pd.notna(d) else float("nan")
    )
    years = days.astype(float) / 365.0
    return years.clip(lower=0.0)


def compute_row_vega(df, risk_free_rate=0.0, source_vega_iv_unit=1.0):
    """Resolve a per-row vega Series for an option chain DataFrame.

    Uses the ``vega`` column where finite; otherwise computes Black-Scholes
    vega from ``implied_volatility`` (with T from quote_time/expiry). Both
    paths are normalized to dollars per share per 1 vol point (see the
    module docstring; ``source_vega_iv_unit`` declares the IV move the
    source vega is quoted against, 1.0 = per 100% move).

    Parameters
    ----------
    df : pandas.DataFrame
        Option chain with the shared contract columns.
    risk_free_rate : float, default 0.0
        Risk-free rate (decimal) used only on the vega-fallback path.
    source_vega_iv_unit : float, default 1.0
        The IV move the source ``vega`` column (and the BS fallback, which
        is per 1.0) is quoted against: 1.0 = per 100% IV move (default),
        0.01 = per 1 vol point. Must be positive.

    Returns
    -------
    tuple (vega_series, missing_mask)
        ``vega_series``: float Series of resolved per-share vegas per 1 vol
        point (0.0 where missing). ``missing_mask``: boolean Series, True
        for rows where vega was NaN *and* implied_volatility was NaN (i.e.
        rows with no usable vega).
    """
    if source_vega_iv_unit <= 0:
        raise ValueError(
            f"source_vega_iv_unit must be positive, got {source_vega_iv_unit!r}"
        )

    supplied = pd.to_numeric(df["vega"], errors="coerce")
    has_supplied = supplied.notna()

    iv = pd.to_numeric(df["implied_volatility"], errors="coerce")
    has_iv = iv.notna()

    needs_bs = (~has_supplied) & has_iv
    bs_vals = pd.Series(0.0, index=df.index, dtype=float)
    if needs_bs.any():
        tte = _years_to_expiry(df["quote_time"], df["expiry"])
        bs_vals = pd.Series(
            bs_vega(
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

    # Normalize to dollars per share per 1 vol point, never mixing units
    # silently: supplied and fallback vega are both per source_vega_iv_unit.
    divisor = VOL_POINT_DIVISOR * source_vega_iv_unit
    return (resolved / divisor).fillna(0.0), missing


def compute_exposure(df, risk_free_rate=0.0, dealer_position="short",
                      source_vega_iv_unit=1.0):
    """Compute per-row dealer vega exposure ($ per 1 vol-point IV move).

    Parameters
    ----------
    df : pandas.DataFrame
        One row per option contract, with the shared 16-column contract
        schema (see ``..capture.schema``).
    risk_free_rate : float, default 0.0
        Risk-free rate (decimal) used only on the vega-fallback path.
    dealer_position : {'short', 'long', 'flat'}, default 'short'
        The explicit dealer-positioning assumption; see the module docstring.
    source_vega_iv_unit : float, default 1.0
        The IV move the source ``vega`` column is quoted against
        (1.0 = per 100% IV move; 0.01 = per 1 vol point).

    Returns
    -------
    tuple (exposure_df, meta)
        ``exposure_df``: copy of the usable rows with added columns
        ``resolved_vega`` (vega actually used, per share per 1 vol point)
        and ``dealer_vex`` (dollar exposure per 1 vol-point IV move, dealer
        convention applied). ``meta``: dict with ``skipped_rows`` (rows
        missing both vega and IV), ``n_rows`` (input rows), and
        ``dealer_position``.
    """
    if dealer_position not in _DEALER_SIGNS:
        raise ValueError(
            f"dealer_position must be one of {sorted(_DEALER_SIGNS)}, "
            f"got {dealer_position!r}"
        )
    sign = _DEALER_SIGNS[dealer_position]

    resolved_vega, missing = compute_row_vega(
        df, risk_free_rate=risk_free_rate, source_vega_iv_unit=source_vega_iv_unit
    )
    skipped = int(missing.sum())

    usable = df.loc[~missing].copy()
    v = resolved_vega.loc[~missing]
    oi = pd.to_numeric(usable["open_interest"], errors="coerce").fillna(0).astype(float)

    customer_vex = v * oi * 100.0
    usable["resolved_vega"] = v.values
    usable["dealer_vex"] = sign * customer_vex.values

    meta = {
        "n_rows": int(len(df)),
        "skipped_rows": skipped,
        "dealer_position": dealer_position,
    }
    return usable, meta
