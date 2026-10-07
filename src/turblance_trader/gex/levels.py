"""Key GEX levels and the GEXProfile for the turblance-trader GEX engine.

Everything here is *dealer-convention* net gamma exposure (see
``exposure.py`` for the positioning assumption), aggregated across the whole
option chain snapshot.

LEVEL DEFINITIONS
-----------------
- ``by_strike``: per-strike dealer GEX, columns
  ``strike, call_gex, put_gex, net_gex``, sorted ascending by strike.
- ``by_expiry``: per-expiry dealer GEX, columns ``expiry, net_gex``.
- ``zero_gamma_flip``: the strike where total net dealer gamma crosses zero.
  Computed by sorting strikes ascending, finding an adjacent pair whose
  net GEX changes sign, and linearly interpolating the zero crossing; if a
  strike's net GEX is (within tolerance) exactly zero, that strike is the
  flip. ``None`` when there is no crossing. Interpretation (EXPERIMENTAL /
  UNVALIDATED): the strike at which dealer hedging is thought to flip
  direction — buying the underlying on one side, selling on the other.
- ``call_wall``: among strikes at or above spot, the strike with the largest
  |call-side| dealer GEX — the overhead resistance zone. ``None`` if no
  strike is at/above spot.
- ``put_wall``: among strikes at or below spot, the strike with the largest
  |put-side| dealer GEX — the support zone. ``None`` if no strike is
  at/below spot.
- ``king_node``: ``{'strike', 'net_gex'}`` of the strike with the largest
  |net GEX| overall — the "center of structural gravity" of the chain.
- ``regime``: a coarse gamma-regime read. 'positive' if total net GEX is
  strongly positive (range-day heuristic), 'negative' if strongly negative
  (trend-day heuristic), otherwise 'mixed' (whipsaw/chop heuristic).
  The threshold is a relative one: total / sum(|net|) >= +0.5 -> 'positive',
  <= -0.5 -> 'negative', else 'mixed'. The 0.5 cutoff is an arbitrary
  starting heuristic, EXPERIMENTAL and UNVALIDATED until backtested — it
  says nothing reliable about future price behavior on its own.

Evidence note: per the project's evidence standard, the *measurements*
(by_strike aggregation, flip interpolation, walls, king node) are mechanical
derivations of the input data; the *interpretations* attached to them
(range-day/trend-day heuristics, wall-as-support/resistance, flip-zone
trading implications) are EXPERIMENTAL/UNVALIDATED until a backtest report
says otherwise.
"""

import numpy as np
import pandas as pd

from .exposure import REQUIRED_COLUMNS, compute_exposure

_ZERO_TOL = 1e-9
_REGIME_THRESHOLD = 0.5  # EXPERIMENTAL/UNVALIDATED — see module docstring.


class GEXProfile:
    """One computed GEX snapshot: aggregated exposures plus key levels.

    Attributes
    ----------
    by_strike : pandas.DataFrame
        Columns ``strike, call_gex, put_gex, net_gex`` (dealer convention),
        sorted ascending by strike.
    by_expiry : pandas.DataFrame
        Columns ``expiry, net_gex`` (dealer convention), sorted by expiry.
    spot : float
        Underlying reference price used for wall placement (NaN if unknown).
    zero_gamma_flip : float | None
        Interpolated strike where net dealer gamma crosses zero.
    call_wall : float | None
        Strike >= spot with max |call_gex|.
    put_wall : float | None
        Strike <= spot with max |put_gex|.
    king_node : dict
        ``{'strike': float|None, 'net_gex': float|None}`` with max |net_gex|.
    regime : str
        'positive' | 'negative' | 'mixed' (EXPERIMENTAL/UNVALIDATED).
    skipped_rows : int
        Input rows skipped for lack of gamma and implied volatility.
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
        zero_gamma_flip,
        call_wall,
        put_wall,
        king_node,
        regime,
        skipped_rows=0,
        n_rows=0,
        dealer_position="short",
    ):
        self.by_strike = by_strike
        self.by_expiry = by_expiry
        self.spot = spot
        self.zero_gamma_flip = zero_gamma_flip
        self.call_wall = call_wall
        self.put_wall = put_wall
        self.king_node = king_node
        self.regime = regime
        self.skipped_rows = skipped_rows
        self.n_rows = n_rows
        self.dealer_position = dealer_position

    def __repr__(self):
        return (
            f"GEXProfile(spot={self.spot}, regime={self.regime!r}, "
            f"strikes={len(self.by_strike)}, flip={self.zero_gamma_flip}, "
            f"king={self.king_node.get('strike')}, skipped={self.skipped_rows})"
        )

    def to_dataframe(self):
        """Return the by_strike frame (strike, call_gex, put_gex, net_gex)."""
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
            "regime": self.regime,
            "zero_gamma_flip": _f(self.zero_gamma_flip),
            "call_wall": _f(self.call_wall),
            "put_wall": _f(self.put_wall),
            "king_node": {
                "strike": _f(self.king_node.get("strike")),
                "net_gex": _f(self.king_node.get("net_gex")),
            },
            "total_net_gex": _f(self.by_strike["net_gex"].sum()),
            "n_strikes": int(len(self.by_strike)),
            "n_rows": int(self.n_rows),
            "skipped_rows": int(self.skipped_rows),
            "dealer_position": self.dealer_position,
        }


def _aggregate(exposure_df):
    """Build by_strike / by_expiry frames from per-row dealer exposure."""
    if exposure_df.empty:
        by_strike = pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])
        by_expiry = pd.DataFrame(columns=["expiry", "net_gex"])
        return by_strike, by_expiry

    work = exposure_df.copy()
    work["strike"] = pd.to_numeric(work["strike"], errors="coerce").astype(float)
    work["dealer_gex"] = pd.to_numeric(work["dealer_gex"], errors="coerce").fillna(0.0)
    is_call = work["option_type"].astype(str).str.lower() == "call"

    work["call_gex"] = np.where(is_call, work["dealer_gex"], 0.0)
    work["put_gex"] = np.where(~is_call, work["dealer_gex"], 0.0)

    by_strike = (
        work.groupby("strike", as_index=False)[["call_gex", "put_gex"]]
        .sum()
        .sort_values("strike")
        .reset_index(drop=True)
    )
    by_strike["net_gex"] = by_strike["call_gex"] + by_strike["put_gex"]

    by_expiry = (
        work.groupby("expiry", as_index=False)["dealer_gex"]
        .sum()
        .rename(columns={"dealer_gex": "net_gex"})
        .sort_values("expiry")
        .reset_index(drop=True)
    )
    return by_strike, by_expiry


def find_zero_gamma_flip(by_strike):
    """Interpolate the strike where total net dealer gamma crosses zero.

    Only strikes with meaningful exposure (|net_gex| >= _ZERO_TOL) take part
    in the search: far-tail strikes with no open interest carry zero GEX and
    must not anchor the crossing (a leading run of zeros is not a flip).
    Among the remaining strikes, sorted ascending, the first adjacent pair
    with a strict sign change in net_gex yields a linearly interpolated
    crossing:
        s* = s_i + (0 - n_i) * (s_j - s_i) / (n_j - n_i).
    Returns None when net GEX never changes sign.

    Interpretation (EXPERIMENTAL/UNVALIDATED): the strike at which dealer
    hedging is thought to flip direction.
    """
    if by_strike is None or by_strike.empty:
        return None
    frame = by_strike[by_strike["net_gex"].abs() >= _ZERO_TOL]
    strikes = frame["strike"].to_numpy(dtype=float)
    nets = frame["net_gex"].to_numpy(dtype=float)
    if len(strikes) < 2:
        return None
    for i in range(len(strikes) - 1):
        if nets[i] * nets[i + 1] < 0:
            s_i, s_j = strikes[i], strikes[i + 1]
            n_i, n_j = nets[i], nets[i + 1]
            return float(s_i + (0.0 - n_i) * (s_j - s_i) / (n_j - n_i))
    return None


def find_walls(by_strike, spot):
    """Locate the call wall and put wall relative to spot.

    call_wall: strike at/above spot with max |call_gex| (overhead zone).
    put_wall:  strike at/below spot with max |put_gex| (support zone).
    Each is None when no strike qualifies. Interpretations are
    EXPERIMENTAL/UNVALIDATED.
    """
    call_wall = put_wall = None
    if by_strike is None or by_strike.empty or spot is None or np.isnan(spot):
        return call_wall, put_wall
    strikes = by_strike["strike"].to_numpy(dtype=float)

    above = by_strike[strikes >= spot]
    if not above.empty:
        idx = above["call_gex"].abs().idxmax()
        call_wall = float(above.loc[idx, "strike"])

    below = by_strike[strikes <= spot]
    if not below.empty:
        idx = below["put_gex"].abs().idxmax()
        put_wall = float(below.loc[idx, "strike"])
    return call_wall, put_wall


def find_king_node(by_strike):
    """Return {'strike', 'net_gex'} of the strike with max |net_gex|.

    The "center of structural gravity" of the chain. For an empty chain,
    both values are None.
    """
    if by_strike is None or by_strike.empty:
        return {"strike": None, "net_gex": None}
    idx = by_strike["net_gex"].abs().idxmax()
    row = by_strike.loc[idx]
    return {"strike": float(row["strike"]), "net_gex": float(row["net_gex"])}


def classify_regime(by_strike):
    """Classify the gamma regime from total vs gross net dealer GEX.

    frac = total_net_gex / sum(|net_gex|).
    frac >= +0.5 -> 'positive' (range-day heuristic),
    frac <= -0.5 -> 'negative' (trend-day heuristic),
    otherwise 'mixed' (whipsaw heuristic); 'mixed' when gross is zero.

    The +/-0.5 threshold is an arbitrary starting heuristic and the regime
    *interpretations* are EXPERIMENTAL/UNVALIDATED until backtested.
    """
    if by_strike is None or by_strike.empty:
        return "mixed"
    nets = by_strike["net_gex"].to_numpy(dtype=float)
    gross = float(np.abs(nets).sum())
    if gross == 0.0:
        return "mixed"
    frac = float(nets.sum()) / gross
    if frac >= _REGIME_THRESHOLD:
        return "positive"
    if frac <= -_REGIME_THRESHOLD:
        return "negative"
    return "mixed"


def compute_profile(df, spot=None, risk_free_rate=0.0, dealer_position="short"):
    """Compute a full GEX profile from an option-chain snapshot.

    Parameters
    ----------
    df : pandas.DataFrame
        One row per option contract with the shared contract columns
        (symbol, quote_time, expiry, strike, option_type, bid, ask, last,
        implied_volatility, open_interest, volume, delta, gamma, theta,
        vega, spot). An empty DataFrame yields an empty profile (no
        exception).
    spot : float, optional
        Underlying reference price for wall placement. Defaults to the
        median of ``df['spot']``.
    risk_free_rate : float, default 0.0
        Risk-free rate (decimal) used only for the gamma-fallback path
        (rows whose gamma is NaN but implied_volatility is present).
    dealer_position : {'short', 'long', 'flat'}, default 'short'
        The explicit dealer-positioning assumption; see ``exposure.py``.

    Returns
    -------
    GEXProfile
        Aggregated exposures plus zero_gamma_flip, call_wall, put_wall,
        king_node, and regime. Rows missing both gamma and implied
        volatility are skipped and counted in ``profile.skipped_rows``.
    """
    if df is None or len(df) == 0:
        empty_strike = pd.DataFrame(columns=["strike", "call_gex", "put_gex", "net_gex"])
        empty_expiry = pd.DataFrame(columns=["expiry", "net_gex"])
        return GEXProfile(
            by_strike=empty_strike,
            by_expiry=empty_expiry,
            spot=float("nan") if spot is None else float(spot),
            zero_gamma_flip=None,
            call_wall=None,
            put_wall=None,
            king_node={"strike": None, "net_gex": None},
            regime="mixed",
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
        df, risk_free_rate=risk_free_rate, dealer_position=dealer_position
    )
    by_strike, by_expiry = _aggregate(exposure_df)

    zero_gamma_flip = find_zero_gamma_flip(by_strike)
    call_wall, put_wall = find_walls(by_strike, spot)
    king_node = find_king_node(by_strike)
    regime = classify_regime(by_strike)

    return GEXProfile(
        by_strike=by_strike,
        by_expiry=by_expiry,
        spot=spot,
        zero_gamma_flip=zero_gamma_flip,
        call_wall=call_wall,
        put_wall=put_wall,
        king_node=king_node,
        regime=regime,
        skipped_rows=meta["skipped_rows"],
        n_rows=meta["n_rows"],
        dealer_position=dealer_position,
    )


def gex_velocity(profile_a, profile_b):
    """Per-strike change in net dealer GEX between two profile snapshots.

    Strikes are aligned on the union of both profiles' strikes; a strike
    present in only one snapshot is treated as 0 in the other (documented
    convention). Positive values mean dealer exposure grew at that strike,
    negative means it unwound — the rate-of-change of the exposure map.

    Parameters
    ----------
    profile_a, profile_b : GEXProfile
        The earlier and later snapshots.

    Returns
    -------
    pandas.Series
        ``net_gex_change = net_gex_b - net_gex_a`` per strike, indexed by
        strike (ascending). Interpretation of velocity as accumulation vs.
        distribution signal is EXPERIMENTAL/UNVALIDATED.
    """
    a = profile_a.to_dataframe().set_index("strike")["net_gex"]
    b = profile_b.to_dataframe().set_index("strike")["net_gex"]
    strikes = sorted(set(a.index) | set(b.index))
    change = pd.Series(
        [float(b.get(s, 0.0) - a.get(s, 0.0)) for s in strikes],
        index=pd.Index(strikes, name="strike"),
        name="net_gex_change",
        dtype=float,
    )
    return change
