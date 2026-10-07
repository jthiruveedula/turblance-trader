"""Black-Scholes Greeks for the turblance-trader GEX/VEX engines.

Clean-room implementation of the public textbook formulas. Gamma is identical
for calls and puts; only delta differs by option type. Vega (shared with the
VEX engine in ``..vex``) is quoted per 1.0 (100%) move of implied volatility,
per share, in the same units as a textbook Vega = S * N'(d1) * sqrt(T).

Edge-case policy (no NaN leaks, no exceptions on degenerate inputs):
- time_to_expiry <= 0  -> gamma 0; delta collapses to the intrinsic step.
- volatility <= 0      -> gamma 0; delta collapses to the intrinsic step.
- spot <= 0 or strike <= 0 -> gamma 0, delta 0.
- NaN inputs propagate to 0 output rather than NaN (finite zeros everywhere).

These functions are written with numpy ops so they accept scalars, lists, and
numpy/pandas Series alike (elementwise).
"""

import math

import numpy as np

_SQRT_2PI = np.sqrt(2.0 * np.pi)
_SQRT_2 = np.sqrt(2.0)

_vec_erf = np.vectorize(math.erf)


def _norm_pdf(x):
    """Standard normal probability density function."""
    return np.exp(-0.5 * np.asarray(x, dtype=float) ** 2) / _SQRT_2PI


def _norm_cdf(x):
    """Standard normal cumulative distribution function (via erf)."""
    return 0.5 * (1.0 + _vec_erf(np.asarray(x, dtype=float) / _SQRT_2))


def _d1(spot, strike, time_to_expiry, volatility, risk_free_rate=0.0):
    """The d1 term of the Black-Scholes formula.

    d1 = [ln(S/K) + (r + sigma^2/2) * T] / (sigma * sqrt(T))
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry, dtype=float)
    volatility = np.asarray(volatility, dtype=float)
    denom = volatility * np.sqrt(time_to_expiry)
    return (np.log(spot / strike) + (risk_free_rate + 0.5 * volatility ** 2) * time_to_expiry) / denom


def gamma(spot, strike, time_to_expiry, volatility, risk_free_rate=0.0):
    """Option gamma via the Black-Scholes formula.

    Gamma = N'(d1) / (S * sigma * sqrt(T)), where N' is the standard normal
    pdf and d1 is defined above. Gamma is the same for calls and puts.

    Parameters
    ----------
    spot : float or array
        Current underlying price S.
    strike : float or array
        Strike price K.
    time_to_expiry : float or array
        Years to expiry T.
    volatility : float or array
        Implied volatility sigma (decimal, e.g. 0.2 for 20%).
    risk_free_rate : float, default 0.0
        Risk-free rate r (decimal).

    Returns
    -------
    float or numpy array
        Gamma (per $1 move in the underlying). Returns 0 wherever the inputs
        are degenerate (T <= 0, sigma <= 0, S <= 0, K <= 0, or NaN); never NaN.
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry, dtype=float)
    volatility = np.asarray(volatility, dtype=float)

    valid = (
        (time_to_expiry > 0)
        & (volatility > 0)
        & (spot > 0)
        & (strike > 0)
        & np.isfinite(spot)
        & np.isfinite(strike)
        & np.isfinite(time_to_expiry)
        & np.isfinite(volatility)
    )

    # Suppress warnings for the invalid region; the mask below discards it.
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = _d1(spot, strike, time_to_expiry, volatility, risk_free_rate)
        raw = _norm_pdf(d1) / (spot * volatility * np.sqrt(time_to_expiry))

    out = np.where(valid, raw, 0.0)
    out = np.where(np.isfinite(out), out, 0.0)
    if out.ndim == 0:
        return float(out)
    return out


def vega(spot, strike, time_to_expiry, volatility, risk_free_rate=0.0):
    """Option vega via the Black-Scholes formula.

    Vega = S * N'(d1) * sqrt(T), where N' is the standard normal pdf and d1
    is defined above. Vega is the same for calls and puts.

    Parameters
    ----------
    spot, strike : float or array
        Underlying price S and strike K.
    time_to_expiry : float or array
        Years to expiry T.
    volatility : float or array
        Implied volatility sigma (decimal, e.g. 0.2 for 20%).
    risk_free_rate : float, default 0.0
        Risk-free rate r (decimal).

    Returns
    -------
    float or numpy array
        Vega in *per-share dollars per 1.0 (100%) move of IV*. Downstream
        code converts to per-1-vol-point ($ per 1% IV move) by dividing by
        100 — see ``turblance_trader.vex.exposure.VOL_POINT_DIVISOR``.
        Returns 0 wherever the inputs are degenerate (T <= 0, sigma <= 0,
        S <= 0, K <= 0, or NaN); never NaN.
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry, dtype=float)
    volatility = np.asarray(volatility, dtype=float)

    valid = (
        (time_to_expiry > 0)
        & (volatility > 0)
        & (spot > 0)
        & (strike > 0)
        & np.isfinite(spot)
        & np.isfinite(strike)
        & np.isfinite(time_to_expiry)
        & np.isfinite(volatility)
    )

    # Suppress warnings for the invalid region; the mask below discards it.
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = _d1(spot, strike, time_to_expiry, volatility, risk_free_rate)
        raw = spot * _norm_pdf(d1) * np.sqrt(time_to_expiry)

    out = np.where(valid, raw, 0.0)
    out = np.where(np.isfinite(out), out, 0.0)
    if out.ndim == 0:
        return float(out)
    return out


def delta(spot, strike, time_to_expiry, volatility, option_type, risk_free_rate=0.0):
    """Option delta via the Black-Scholes formula.

    Call delta = N(d1); put delta = N(d1) - 1.

    Parameters
    ----------
    option_type : 'call' or 'put' (array-like of str allowed)

    Returns
    -------
    float or numpy array
        Delta. Degenerate inputs (T <= 0, sigma <= 0, S <= 0, K <= 0, NaN)
        collapse to the intrinsic step: call -> 1.0 if S > K else 0.0,
        put -> -1.0 if S < K else 0.0; never NaN.
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    time_to_expiry = np.asarray(time_to_expiry, dtype=float)
    volatility = np.asarray(volatility, dtype=float)

    valid = (
        (time_to_expiry > 0)
        & (volatility > 0)
        & (spot > 0)
        & (strike > 0)
        & np.isfinite(spot)
        & np.isfinite(strike)
        & np.isfinite(time_to_expiry)
        & np.isfinite(volatility)
    )

    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = _d1(spot, strike, time_to_expiry, volatility, risk_free_rate)
        n_d1 = _norm_cdf(d1)

    is_call = np.char.equal(np.asarray(option_type, dtype=str), "call")

    call_delta = np.where(valid, n_d1, np.where(spot > strike, 1.0, 0.0))
    put_delta = np.where(valid, n_d1 - 1.0, np.where(spot < strike, -1.0, 0.0))
    out = np.where(is_call, call_delta, put_delta)
    out = np.where(np.isfinite(out), out, 0.0)
    if out.ndim == 0:
        return float(out)
    return out
