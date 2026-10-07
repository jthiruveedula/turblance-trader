"""Key VEX levels and the VEXProfile for the turblance-trader VEX engine.

Everything here is *dealer-convention* net vega exposure (see
``exposure.py`` for the positioning assumption and the $/1-vol-point unit
contract), aggregated across the whole option chain snapshot.

LEVEL DEFINITIONS
-----------------
- ``by_strike``: per-strike dealer VEX, columns
  ``strike, call_vex, put_vex, net_vex``, sorted ascending by strike.
- ``by_expiry``: per-expiry dealer VEX, columns ``expiry, net_vex``.
- ``vega_flip``: the strike where total net dealer vega crosses zero.
  Computed by sorting strikes ascending, finding an adjacent pair whose
  net VEX changes sign, and linearly interpolating the zero crossing; if a
  strike's net VEX is (within tolerance) exactly zero, that strike is the
  flip. ``None`` when there is no crossing. Interpretation (EXPERIMENTAL /
  UNVALIDATED): the strike at which dealer vol-hedging flow is thought to
  flip — gamma-style dealer hedging analog for volatility exposure.
- ``call_vega_wall``: among strikes at or above spot, the strike with the
  largest |call-side| dealer VEX — the overhead zone where vol supply is
  concentrated. ``None`` if no strike is at/above spot.
- ``put_vega_wall``: among strikes at or below spot, the strike with the
  largest |put-side| dealer VEX — the downside zone where vol supply is
  concentrated. ``None`` if no strike is at/below spot.
- ``vex_king``: ``{'strike', 'net_vex'}`` of the strike with the largest
  |net VEX| overall — the "center of vol gravity" of the chain.
- ``vol_regime``: a coarse volatility-exposure regime read. 'positive' if
  total net VEX is strongly positive, 'negative' if strongly negative,
  otherwise 'mixed'. The threshold is a relative one: total / sum(|net|)
  >= +0.5 -> 'positive', <= -0.5 -> 'negative', else 'mixed'. The 0.5 cutoff
  is an arbitrary starting heuristic, EXPERIMENTAL and UNVALIDATED until
  backtested — it says nothing reliable about future volatility behavior on
  its own.

Evidence note: per the project's evidence standard, the *measurements*
(by_strike aggregation, flip interpolation, walls, king node) are mechanical
derivations of the input data; the *interpretations* attached to them
(wall-as-vol-supply-zone, vol-regime reads, flip-zone flow implications)
are EXPERIMENTAL/UNVALIDATED until a backtest report says otherwise.
"""

import numpy as np
import pandas as pd

from .exposure import REQUIRED_COLUMNS, compute_exposure

_ZERO_TOL = 1e-9
_REGIME_THRESHOLD = 0.5  # EXPERIMENTAL/UNVALIDATED — see module docstring.


class VEXProfile:
    """One computed VEX snapshot: aggregated exposures plus key levels.

    Attributes
    ----------
    by_strike : pandas.DataFrame
        Columns ``strike, call_vex, put_vex, net_vex`` (dealer convention),
        sorted ascending by strike.
    by_expiry : pandas.DataFrame
        Columns ``expiry, net_vex`` (dealer convention), sorted by expiry.
    spot : float
        Underlying reference price used for wall placement (NaN if unknown).
    vega_flip : float | None
        Interpolated strike where net dealer vega crosses zero.
    call_vega_wall : float | None
        Strike >= spot with max |call_vex|.
    put_vega_wall : float | None
        Strike <= spot with max |put_vex|.
    vex_king : dict
        ``{'strike': float|None, 'net_vex': float|None}`` with max |net_vex|.
    vol_regime : str
        'positive' | 'negative' | 'mixed' (EXPERIMENTAL/UNVALIDATED).
    skipped_rows : int
        Input rows skipped for lack of vega and implied volatility.
    n_rows : int
        Input rows supplied.
    dealer_position : str
        The dealer-positioning assumption used ('short'|'long'|'flat').
    """

    def __init__(
        self,
        by_strike,
        by_expiry,
        spot,
        vega_flip,
        call_vega_wall,
        put_vega_wall,
        vex_king,
        vol_regime,
        skipped_rows=0,
        n_rows=0,
        dealer_position="short",
    ):
        self.by_strike = by_strike
        self.by_expiry = by_expiry
        self.spot = spot
        self.vega_flip = vega_flip
        self.call_vega_wall = call_vega_wall
        self.put_vega_wall = put_vega_wall
        self.vex_king = vex_king
        self.vol_regime = vol_regime
        self.skipped_rows = skipped_rows
        self.n_rows = n_rows
        self.dealer_position = dealer_position

    def __repr__(self):
        return (
            f"VEXProfile(spot={self.spot}, vol_regime={self.vol_regime!r}, "
            f"strikes={len(self.by_strike)}, vega_flip={self.vega_flip}, "
            f"king={self.vex_king.get('strike')}, skipped={self.skipped_rows})"
        )

    def to_dataframe(self):
        """Return the by_strike frame (strike, call_vex, put_vex, net_vex)."""
        return self.by_strike.copy()

    def to_dict(self):
        """JSON-serializable summary of the profile.

        NaN values (e.g. unknown spot on an empty profile) become None so
        the result is safe for ``json.dumps``.
        """

        def _f(x):
            if x is None:
                return None
            x = float(x)
            return None if np.isnan(x) else x

        return {
            "spot": _f(self.spot),
            "vol_regime": self.vol_regime,
            "vega_flip": _f(self.vega_flip),
            "call_vega_wall": _f(self.call_vega_wall),
            "put_vega_wall": _f(self.put_vega_wall),
            "vex_king": {
                "strike": _f(self.vex_king.get("strike")),
                "net_vex": _f(self.vex_king.get("net_vex")),
            },
            "total_net_vex": _f(self.by_strike["net_vex"].sum()),
            "n_strikes": int(len(self.by_strike)),
            "n_rows": int(self.n_rows),
            "skipped_rows": int(self.skipped_rows),
            "dealer_position": self.dealer_position,
        }


def _aggregate(exposure_df):
    """Build by_strike / by_expiry frames from per-row dealer exposure."""
    if exposure_df.empty:
        by_strike = pd.DataFrame(columns=["strike", "call_vex", "put_vex", "net_vex"])
        by_expiry = pd.DataFrame(columns=["expiry", "net_vex"])
        return by_strike, by_expiry

    work = exposure_df.copy()
    work["strike"] = pd.to_numeric(work["strike"], errors="coerce").astype(float)
    work["dealer_vex"] = pd.to_numeric(work["dealer_vex"], errors="coerce").fillna(0.0)
    is_call = work["option_type"].astype(str).str.lower() == "call"

    work["call_vex"] = np.where(is_call, work["dealer_vex"], 0.0)
    work["put_vex"] = np.where(~is_call, work["dealer_vex"], 0.0)

    by_strike = (
        work.groupby("strike", as_index=False)[["call_vex", "put_vex"]]
        .sum()
        .sort_values("strike")
        .reset_index(drop=True)
    )
    by_strike["net_vex"] = by_strike["call_vex"] + by_strike["put_vex"]

    by_expiry = (
        work.groupby("expiry", as_index=False)["dealer_vex"]
        .sum()
        .rename(columns={"dealer_vex": "net_vex"})
        .sort_values("expiry")
        .reset_index(drop=True)
    )
    return by_strike, by_expiry


def find_vega_flip(by_strike):
    """Interpolate the strike where total net dealer vega crosses zero.

    Only strikes with meaningful exposure (|net_vex| >= _ZERO_TOL) take part
    in the search: far-tail strikes with no open interest carry zero VEX and
    must not anchor the crossing (a leading run of zeros is not a flip).
    Among the remaining strikes, sorted ascending, the first adjacent pair
    with a strict sign change in net_vex yields a linearly interpolated
    crossing:
        s* = s_i + (0 - n_i) * (s_j - s_i) / (n_j - n_i).
    Returns None when net VEX never changes sign.

    Interpretation (EXPERIMENTAL/UNVALIDATED): the strike at which dealer
    vol-hedging flow is thought to flip.
    """
    if by_strike is None or by_strike.empty:
        return None
    frame = by_strike[by_strike["net_vex"].abs() >= _ZERO_TOL]
    strikes = frame["strike"].to_numpy(dtype=float)
    nets = frame["net_vex"].to_numpy(dtype=float)
    if len(strikes) < 2:
        return None
    for i in range(len(strikes) - 1):
        if nets[i] * nets[i + 1] < 0:
            s_i, s_j = strikes[i], strikes[i + 1]
            n_i, n_j = nets[i], nets[i + 1]
            return float(s_i + (0.0 - n_i) * (s_j - s_i) / (n_j - n_i))
    return None


def find_vega_walls(by_strike, spot):
    """Locate the call vega wall and put vega wall relative to spot.

    call_vega_wall: strike at/above spot with max |call_vex| (overhead vol
    zone). put_vega_wall: strike at/below spot with max |put_vex|
    (downside vol zone). Each is None when no strike qualifies.
    Interpretations are EXPERIMENTAL/UNVALIDATED.
    """
    call_vega_wall = put_vega_wall = None
    if by_strike is None or by_strike.empty or spot is None or np.isnan(spot):
        return call_vega_wall, put_vega_wall
    strikes = by_strike["strike"].to_numpy(dtype=float)

    above = by_strike[strikes >= spot]
    if not above.empty:
        idx = above["call_vex"].abs().idxmax()
        call_vega_wall = float(above.loc[idx, "strike"])

    below = by_strike[strikes <= spot]
    if not below.empty:
        idx = below["put_vex"].abs().idxmax()
        put_vega_wall = float(below.loc[idx, "strike"])
    return call_vega_wall, put_vega_wall


def find_vex_king_node(by_strike):
    """Return {'strike', 'net_vex'} of the strike with max |net_vex|.

    The "center of vol gravity" of the chain. For an empty chain, both
    values are None.
    """
    if by_strike is None or by_strike.empty:
        return {"strike": None, "net_vex": None}
    idx = by_strike["net_vex"].abs().idxmax()
    row = by_strike.loc[idx]
    return {"strike": float(row["strike"]), "net_vex": float(row["net_vex"])}


def classify_vol_regime(by_strike):
    """Classify the vol regime from total vs gross net dealer VEX.

    frac = total_net_vex / sum(|net_vex|).
    frac >= +0.5 -> 'positive', frac <= -0.5 -> 'negative', otherwise
    'mixed'; 'mixed' when gross is zero.

    The +/-0.5 threshold is an arbitrary starting heuristic and the regime
    *interpretations* are EXPERIMENTAL/UNVALIDATED until backtested.
    """
    if by_strike is None or by_strike.empty:
        return "mixed"
    nets = by_strike["net_vex"].to_numpy(dtype=float)
    gross = float(np.abs(nets).sum())
    if gross == 0.0:
        return "mixed"
    frac = float(nets.sum()) / gross
    if frac >= _REGIME_THRESHOLD:
        return "positive"
    if frac <= -_REGIME_THRESHOLD:
        return "negative"
    return "mixed"


def compute_vex_profile(df, spot=None, risk_free_rate=0.0, dealer_position="short",
                        source_vega_iv_unit=1.0):
    """Compute a full VEX profile from an option-chain snapshot.

    Parameters
    ----------
    df : pandas.DataFrame
        One row per option contract with the shared 16-column contract
        schema (see ``..capture.schema``). An empty DataFrame yields an
        empty profile (no exception).
    spot : float, optional
        Underlying reference price for wall placement. Defaults to the
        median of ``df['spot']``.
    risk_free_rate : float, default 0.0
        Risk-free rate (decimal) used only for the vega-fallback path
        (rows whose vega is NaN but implied_volatility is present).
    dealer_position : {'short', 'long', 'flat'}, default 'short'
        The explicit dealer-positioning assumption; see ``exposure.py``.
    source_vega_iv_unit : float, default 1.0
        The IV move the source ``vega`` column is quoted against
        (1.0 = per 100% IV move; 0.01 = per 1 vol point).

    Returns
    -------
    VEXProfile
        Aggregated exposures plus vega_flip, call_vega_wall,
        put_vega_wall, vex_king, and vol_regime. Rows missing both vega
        and implied volatility are skipped and counted in
        ``profile.skipped_rows``.
    """
    if df is None or len(df) == 0:
        empty_strike = pd.DataFrame(
            columns=["strike", "call_vex", "put_vex", "net_vex"]
        )
        empty_expiry = pd.DataFrame(columns=["expiry", "net_vex"])
        return VEXProfile(
            by_strike=empty_strike,
            by_expiry=empty_expiry,
            spot=float("nan") if spot is None else float(spot),
            vega_flip=None,
            call_vega_wall=None,
            put_vega_wall=None,
            vex_king={"strike": None, "net_vex": None},
            vol_regime="mixed",
            skipped_rows=0,
            n_rows=0,
            dealer_position=dealer_position,
        )

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        raise ValueError(f"DataFrame is missing required columns: {missing_cols}")

    if spot is None:
        spot = float(pd.to_numeric(df["spot"], errors="coerce").median())
    else:
        spot = float(spot)

    exposure_df, meta = compute_exposure(
        df,
        risk_free_rate=risk_free_rate,
        dealer_position=dealer_position,
        source_vega_iv_unit=source_vega_iv_unit,
    )
    by_strike, by_expiry = _aggregate(exposure_df)

    vega_flip = find_vega_flip(by_strike)
    call_vega_wall, put_vega_wall = find_vega_walls(by_strike, spot)
    vex_king = find_vex_king_node(by_strike)
    vol_regime = classify_vol_regime(by_strike)

    return VEXProfile(
        by_strike=by_strike,
        by_expiry=by_expiry,
        spot=spot,
        vega_flip=vega_flip,
        call_vega_wall=call_vega_wall,
        put_vega_wall=put_vega_wall,
        vex_king=vex_king,
        vol_regime=vol_regime,
        skipped_rows=meta["skipped_rows"],
        n_rows=meta["n_rows"],
        dealer_position=dealer_position,
    )


def vex_velocity(profile_a, profile_b):
    """Per-strike change in net dealer VEX between two profile snapshots.

    Strikes are aligned on the union of both profiles' strikes; a strike
    present in only one snapshot is treated as 0 in the other (documented
    convention). Positive values mean dealer exposure grew at that strike,
    negative means it unwound — the rate-of-change of the exposure map.

    Parameters
    ----------
    profile_a, profile_b : VEXProfile
        The earlier and later snapshots.

    Returns
    -------
    pandas.Series
        ``net_vex_change = net_vex_b - net_vex_a`` per strike, indexed by
        strike (ascending). Interpretation of velocity as accumulation vs.
        distribution signal is EXPERIMENTAL/UNVALIDATED.
    """
    a = profile_a.to_dataframe().set_index("strike")["net_vex"]
    b = profile_b.to_dataframe().set_index("strike")["net_vex"]
    strikes = sorted(set(a.index) | set(b.index))
    change = pd.Series(
        [float(b.get(s, 0.0) - a.get(s, 0.0)) for s in strikes],
        index=pd.Index(strikes, name="strike"),
        name="net_vex_change",
        dtype=float,
    )
    return change
