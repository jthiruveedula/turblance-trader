"""Flow profile: unusual-activity + sweep proxies rolled into one object.

Clean-room implementation for the turblance-trader flow engine. The
measurements (volume/OI z-scores, sweep-like clusters) are mechanical
derivations of the input chain snapshot; every *interpretation* attached
to them is EXPERIMENTAL and UNVALIDATED until a backtest report says
otherwise. Do not trade on these outputs.

PROFILE CONTRACT (for the later backtest step — duck-typed, keep names EXACT)
-----------------------------------------------------------------------------
``compute_flow_profile(df, prev_df=None, baselines=None)`` returns a
``FlowProfile`` with:

- ``.by_strike`` : DataFrame with columns
  ``strike, call_volume, put_volume, volume_z, oi_change, sweep_score,
  unusual_score`` — one row per strike, ascending.
- ``.by_expiry`` : DataFrame with columns
  ``expiry, total_volume, unusual_score`` (+ ``sweep_score`` extra) —
  one row per expiry, ascending.
- ``.spot`` : float (median of the snapshot's spot column; NaN if unknown).
- ``.unusual_strikes`` : list of dicts, each with
  ``strike, option_type, score, reason`` (+ ``experimental``).
- ``.sweeps`` : list of dicts, each with
  ``expiry, direction, strikes_hit, volume, score`` (+ extras; a sweep
  spanning weeklies + monthlies is ONE event — see below).
- ``.to_dict()`` : JSON-serializable summary of the whole profile.

MULTI-EXPIRY ROLLUP
-------------------
``sweep.detect_sweeps`` merges same-direction/side clusters whose strike
ranges overlap across expiries into a single event, so a sweep across
weeklies + monthlies reads as one footprint. The event's ``expiry`` is the
anchor expiry (largest volume contributor); all expiries are preserved in
``expiries`` / ``strikes_hit_by_expiry``. Per-expiry ``unusual_score`` in
``.by_expiry`` is the max strike unusual_score within that expiry, so a
loud strike lifts its whole expiry's read.

COLD START
----------
With ``baselines=None`` (and/or no ``prev_df``), contracts carry
"insufficient baseline": scores are NaN, ``unusual_strikes`` is empty, and
sweeps fall back to the absolute-volume burst proxy (flagged via
``used_burst_fallback``). Nothing is fabricated.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from ..capture.schema import validate_schema
from .sweep import detect_sweeps
from .unusual_activity import score_unusual_activity

_BY_STRIKE_COLUMNS = [
    "strike",
    "call_volume",
    "put_volume",
    "volume_z",
    "oi_change",
    "sweep_score",
    "unusual_score",
]

_BY_EXPIRY_COLUMNS = [
    "expiry",
    "total_volume",
    "unusual_score",
    "sweep_score",
]


def _safe_float(x):
    """float(x) or NaN; never raises on junk input."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def _nanmean(values):
    vals = [v for v in values
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return float("nan")
    return float(np.mean(vals))


def _nansum(values):
    vals = [v for v in values
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return float("nan")
    return float(sum(vals))


class FlowProfile:
    """One computed flow snapshot: unusual activity + sweep proxies.

    Attribute names are the backtest contract — do not rename them.
    See the module docstring for the full contract.
    """

    def __init__(self, by_strike, by_expiry, spot, unusual_strikes, sweeps,
                 n_rows=0, n_contracts_scored=0, cold_start=True,
                 unusual_threshold=2.0):
        self.by_strike = by_strike
        self.by_expiry = by_expiry
        self.spot = spot
        self.unusual_strikes = unusual_strikes
        self.sweeps = sweeps
        self.n_rows = n_rows
        self.n_contracts_scored = n_contracts_scored
        self.cold_start = cold_start
        self.unusual_threshold = unusual_threshold

    def __repr__(self):
        return (
            f"FlowProfile(spot={self.spot}, strikes={len(self.by_strike)}, "
            f"unusual={len(self.unusual_strikes)}, sweeps={len(self.sweeps)}, "
            f"cold_start={self.cold_start})"
        )

    def to_dict(self):
        """JSON-serializable summary of the profile.

        NaN values become None so the result is safe for ``json.dumps``.
        """

        def _f(x):
            if x is None:
                return None
            try:
                x = float(x)
            except (TypeError, ValueError):
                return None
            return None if math.isnan(x) else x

        def _clean(frame):
            records = []
            for rec in frame.to_dict("records"):
                records.append({k: _f(v) if not isinstance(v, str) else v
                                for k, v in rec.items()})
            return records

        return {
            "spot": _f(self.spot),
            "n_rows": int(self.n_rows),
            "n_contracts_scored": int(self.n_contracts_scored),
            "cold_start": bool(self.cold_start),
            "unusual_threshold": float(self.unusual_threshold),
            "n_unusual_strikes": len(self.unusual_strikes),
            "n_sweeps": len(self.sweeps),
            "unusual_strikes": [
                {
                    "strike": _f(u["strike"]),
                    "option_type": u["option_type"],
                    "score": _f(u["score"]),
                    "reason": u["reason"],
                    "experimental": True,
                }
                for u in self.unusual_strikes
            ],
            "sweeps": [
                {
                    "expiry": s["expiry"],
                    "direction": s["direction"],
                    "strikes_hit": [_f(x) for x in s["strikes_hit"]],
                    "volume": _f(s["volume"]),
                    "score": _f(s["score"]),
                    "expiries": list(s.get("expiries", [s["expiry"]])),
                    "side": s.get("side"),
                    "n_strikes": s.get("n_strikes"),
                    "used_burst_fallback": bool(
                        s.get("used_burst_fallback", False)),
                    "proxy": True,
                    "experimental": True,
                }
                for s in self.sweeps
            ],
            "by_strike": _clean(self.by_strike),
            "by_expiry": _clean(self.by_expiry),
        }


def _build_unusual_strikes(scored, unusual_threshold):
    """Per-contract unusual entries: strike, option_type, score, reason.

    Only contracts WITH a usable baseline can be unusual — cold-start
    rows (NaN score) are skipped, never guessed at.
    """
    out = []
    for row in scored.to_dict("records"):
        score = _safe_float(row.get("contract_score"))
        if math.isnan(score) or score < unusual_threshold:
            continue
        if not bool(row.get("has_baseline")):
            continue
        parts = []
        vz = _safe_float(row.get("volume_z"))
        if not math.isnan(vz) and vz >= unusual_threshold:
            parts.append(f"volume z={vz:.1f}")
        oz = _safe_float(row.get("oi_z"))
        if not math.isnan(oz) and oz >= unusual_threshold:
            parts.append(f"open-interest z={oz:.1f}")
        oc = _safe_float(row.get("oi_change"))
        if not math.isnan(oc) and oc != 0.0:
            parts.append(f"OI change {oc:+.0f} contracts vs prior snapshot")
        reason = ("; ".join(parts) if parts
                  else f"composite score {score:.1f} above threshold")
        src = row.get("baseline_source")
        if src == "bucket":
            reason += " (bucket baseline: thin contract history)"
        out.append({
            "strike": _safe_float(row.get("strike")),
            "option_type": str(row.get("option_type")),
            "score": float(score),
            "reason": "EXPERIMENTAL — " + reason,
            "experimental": True,
        })
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


def _aggregate_by_strike(scored, sweeps):
    """Per-strike rollup with the exact contract column set."""
    if scored.empty:
        return pd.DataFrame(columns=_BY_STRIKE_COLUMNS)

    work = scored.copy()
    work["_strike"] = pd.to_numeric(work["strike"], errors="coerce")
    work["_volume"] = pd.to_numeric(work["volume"], errors="coerce")
    work["_is_call"] = work["option_type"].astype(str).str.lower() == "call"

    # Max sweep score touching each strike (any expiry of the event).
    sweep_at_strike = {}
    for sw in sweeps:
        for s in sw["strikes_hit"]:
            key = round(float(s), 6)
            sweep_at_strike[key] = max(
                sweep_at_strike.get(key, 0.0), float(sw["score"]))

    rows = []
    for strike, group in work.groupby("_strike", sort=True):
        calls = group[group["_is_call"]]
        puts = group[~group["_is_call"]]
        call_vol = _nansum(
            [0.0 if math.isnan(_safe_float(v)) else _safe_float(v)
             for v in calls["_volume"]])
        put_vol = _nansum(
            [0.0 if math.isnan(_safe_float(v)) else _safe_float(v)
             for v in puts["_volume"]])
        rows.append({
            "strike": float(strike),
            "call_volume": 0.0 if math.isnan(call_vol) else call_vol,
            "put_volume": 0.0 if math.isnan(put_vol) else put_vol,
            "volume_z": _nanmean(
                [_safe_float(v) for v in group["volume_z"]]),
            "oi_change": _nansum(
                [_safe_float(v) for v in group["oi_change"]]),
            "sweep_score": float(
                sweep_at_strike.get(round(float(strike), 6), 0.0)),
            # Strike unusual score: sum of the call+put contract scores
            # (NaN scores contribute nothing; all-NaN -> NaN).
            "unusual_score": _nansum(
                [0.0 if math.isnan(_safe_float(v)) else _safe_float(v)
                 for v in group["contract_score"]]
            ) if any(not math.isnan(_safe_float(v))
                     for v in group["contract_score"])
            else float("nan"),
        })
    frame = pd.DataFrame(rows, columns=_BY_STRIKE_COLUMNS)
    return frame.sort_values("strike").reset_index(drop=True)


def _by_expiry_from_scored(scored, by_strike, sweeps):
    """Per-expiry rollup: total volume + max strike unusual_score.

    ``unusual_score`` per expiry is the MAX of its strikes' unusual
    scores (documented choice): one loud strike lifts its expiry's read,
    which is also how the multi-expiry sweep rollup surfaces here — a
    sweep touching several expiries raises each of them.
    """
    if scored.empty:
        return pd.DataFrame(columns=_BY_EXPIRY_COLUMNS)
    work = scored.copy()
    work["_volume"] = pd.to_numeric(work["volume"], errors="coerce").fillna(0.0)

    strike_score = {
        round(float(r["strike"]), 6): _safe_float(r["unusual_score"])
        for r in by_strike.to_dict("records")
    }
    sweep_by_expiry = {}
    for sw in sweeps:
        for exp in sw.get("expiries", [sw["expiry"]]):
            sweep_by_expiry[exp] = max(
                sweep_by_expiry.get(exp, 0.0), float(sw["score"]))

    rows = []
    for expiry, group in work.groupby("expiry", sort=True):
        scores = [
            strike_score.get(round(float(s), 6), float("nan"))
            for s in pd.to_numeric(group["strike"],
                                   errors="coerce").dropna().unique()
        ]
        rows.append({
            "expiry": str(expiry),
            "total_volume": float(group["_volume"].sum()),
            "unusual_score": _nanmean(scores),
            "sweep_score": float(sweep_by_expiry.get(str(expiry), 0.0)),
        })
    frame = pd.DataFrame(rows, columns=_BY_EXPIRY_COLUMNS)
    return frame.sort_values("expiry").reset_index(drop=True)


def compute_flow_profile(df, prev_df=None, baselines=None,
                         unusual_threshold=2.0,
                         min_strikes=3,
                         volume_z_min=2.0,
                         lift_threshold=0.75,
                         hit_threshold=0.25,
                         burst_volume_min=500):
    """Compute a full flow profile from an option-chain snapshot.

    Parameters
    ----------
    df : pandas.DataFrame
        Current snapshot in the shared 16-column contract schema
        (``capture/schema.py``); validated, ``SchemaError`` on violation.
    prev_df : pandas.DataFrame, optional
        Previous snapshot (same schema) for the ``oi_change`` column.
    baselines : BaselineSet, optional
        From ``unusual_activity.compute_baselines``. None means cold
        start: all unusual scores NaN, ``unusual_strikes`` empty, sweeps
        on the absolute-volume burst proxy (flagged).
    unusual_threshold : float, default 2.0
        Minimum contract score to list a strike in ``unusual_strikes``.
    min_strikes, volume_z_min, lift_threshold, hit_threshold,
    burst_volume_min
        Sweep-proxy knobs; see ``sweep.detect_sweeps``.

    Returns
    -------
    FlowProfile
        The backtest-contract object: ``.by_strike``, ``.by_expiry``,
        ``.spot``, ``.unusual_strikes``, ``.sweeps``, ``.to_dict()``.
        Interpretations are EXPERIMENTAL/UNVALIDATED.
    """
    validate_schema(df)
    if prev_df is not None:
        validate_schema(prev_df)

    scored = score_unusual_activity(df, prev_df=prev_df, baselines=baselines)
    sweeps = detect_sweeps(
        df,
        scored_df=scored,
        min_strikes=min_strikes,
        volume_z_min=volume_z_min,
        lift_threshold=lift_threshold,
        hit_threshold=hit_threshold,
        burst_volume_min=burst_volume_min,
    )

    by_strike = _aggregate_by_strike(scored, sweeps)
    by_expiry = _by_expiry_from_scored(scored, by_strike, sweeps)
    unusual_strikes = _build_unusual_strikes(scored, unusual_threshold)

    spot_raw = pd.to_numeric(scored["spot"], errors="coerce")
    spot = float(spot_raw.median()) if len(spot_raw) else float("nan")

    cold_start = baselines is None or bool(
        (~scored["has_baseline"]).all()) if len(scored) else True
    n_scored = int(scored["has_baseline"].sum()) if len(scored) else 0

    return FlowProfile(
        by_strike=by_strike,
        by_expiry=by_expiry,
        spot=spot,
        unusual_strikes=unusual_strikes,
        sweeps=sweeps,
        n_rows=int(len(df)),
        n_contracts_scored=n_scored,
        cold_start=bool(cold_start),
        unusual_threshold=float(unusual_threshold),
    )
