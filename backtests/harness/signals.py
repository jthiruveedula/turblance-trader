"""Candidate GEX signals for the backtest harness.

Each signal is a pure function of the form::

    signal(profile, spot, context) -> dict(levels, bias, rationale, ...)

* ``profile`` — a GEX profile object implementing the shared engine contract:
  ``.by_strike``, ``.by_expiry``, ``.spot``, ``.zero_gamma_flip``,
  ``.call_wall``, ``.put_wall`` (float|None), ``.king_node`` (dict),
  ``.regime`` (str), ``.to_dict()``. Duck-typed so the harness can be tested
  before the engine exists.
* ``spot`` — current underlying price; falls back to ``profile.spot``.
* ``context`` — caller-owned dict for state that must persist across
  snapshots within a session (e.g. touch counts). Use ``new_context()`` to
  create one. The signal functions hold no global state themselves.

STATUS: EXPERIMENTAL — every signal in this module is an untested hypothesis
until it passes validation on real forward-captured data. Do not trade on
these outputs. Do not present them as reliable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Published reaction-probability claim under test (first touch strongest):
# touch 1 -> 0.80, touch 2 -> 0.66, touch 3 -> 0.33, touch 4+ -> 0.10.
# These numbers are the CLAIM, not an established fact — the tap-decay
# backtest exists precisely to measure whether they hold.
PREDICTED_REACTION_PROB = {1: 0.80, 2: 0.66, 3: 0.33}
PREDICTED_REACTION_PROB_4PLUS = 0.10

# Gamma-regime -> day-type forecast map under test.
REGIME_FORECAST = {
    "positive": ("range", "Positive gamma: expect a range day — fade the edges, avoid midpoints."),
    "negative": ("trend", "Negative gamma: expect a trend day — momentum over fades."),
    "mixed": ("whipsaw", "Mixed gamma: expect whipsaw — fade extremes or stand aside."),
}


def new_context(touch_tol_pct: float = 0.05) -> Dict[str, Any]:
    """Fresh per-session caller context for stateful signals.

    ``touch_tol_pct``: how close (percent of spot) price must come to a node
    level to count as a "touch".
    """
    return {
        "touch_tol_pct": float(touch_tol_pct),
        "touches": {},        # node name -> number of touches this session
        "last_touch": None,   # node name touched on the previous snapshot
    }


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _king_strike(profile: Any) -> Optional[float]:
    """King-node strike: largest-|exposure| strike per the profile contract."""
    king = getattr(profile, "king_node", None)
    if isinstance(king, dict):
        strike = _as_float(king.get("strike"))
        if strike is not None:
            return strike
    elif king is not None:
        strike = _as_float(getattr(king, "strike", None))
        if strike is not None:
            return strike
    # Fallback: derive from by_strike (strike, call_gex, put_gex, net_gex).
    by_strike = getattr(profile, "by_strike", None)
    if by_strike is not None and len(by_strike):
        row = by_strike.loc[by_strike["net_gex"].abs().idxmax()]
        return _as_float(row["strike"])
    return None


def _node_levels(profile: Any) -> Dict[str, Optional[float]]:
    """Named node levels from the profile: king, call_wall, put_wall."""
    return {
        "king": _king_strike(profile),
        "call_wall": _as_float(getattr(profile, "call_wall", None)),
        "put_wall": _as_float(getattr(profile, "put_wall", None)),
    }


def _resolve_spot(profile: Any, spot: Optional[float]) -> float:
    if spot is not None:
        return float(spot)
    s = _as_float(getattr(profile, "spot", None))
    if s is None:
        raise ValueError("No spot supplied and profile.spot is unavailable.")
    return s


def _base_result(name: str) -> Dict[str, Any]:
    return {
        "signal": name,
        "experimental": True,  # unvalidated until the backtest report passes
        "levels": [],
        "bias": "neutral",
        "rationale": "",
        "details": {},
    }


def king_magnet(profile: Any, spot: Optional[float] = None,
                context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """King-node magnet: price is drawn to the highest-|exposure| strike into the close.

    EXPERIMENTAL — hypothesis only. The claim under test is that, per session,
    price gravitates toward the King node (largest |exposure| strike) ahead of
    the close because dealer hedging flows mechanically pull price there.
    Validation: ``gex-king-magnet.md`` — fraction of sessions where price
    touched the King node before the close, in-sample vs out-of-sample.

    Returns levels=[king strike], bias toward the node from current spot
    ('bullish' when the node is above spot, 'bearish' when below,
    'neutral' when spot is at the node or no node is identifiable).
    """
    out = _base_result("king_magnet")
    s = _resolve_spot(profile, spot)
    king = _king_strike(profile)
    if king is None:
        out["rationale"] = (
            "No King node identifiable in this profile — no magnet signal "
            "(experimental; no position implied)."
        )
        return out
    out["levels"] = [king]
    distance_pct = (king - s) / s * 100.0
    if abs(distance_pct) < 1e-9:
        out["bias"] = "neutral"
    elif king > s:
        out["bias"] = "bullish"
    else:
        out["bias"] = "bearish"
    out["details"] = {
        "king_strike": king,
        "spot": s,
        "distance_pct": round(distance_pct, 4),
    }
    out["rationale"] = (
        f"EXPERIMENTAL hypothesis: King node at {king:.2f} is "
        f"{abs(distance_pct):.2f}% {'above' if king > s else 'below' if king < s else 'at'} "
        f"spot {s:.2f}; bias {out['bias']} toward the node into the close. "
        "Unvalidated — see backtests/reports/gex-king-magnet.md."
    )
    return out


def node_tap_decay(profile: Any, spot: Optional[float] = None,
                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Node tap-decay: reaction probability falls with each touch of a node.

    EXPERIMENTAL — hypothesis only. Tracks touches of the King node, call
    wall, and put wall across snapshots within a session (state lives in the
    caller-supplied ``context`` dict; create with ``new_context()`` and pass
    the same object for every snapshot of the session). The published claim
    under test: reaction probability decays 0.80 -> 0.66 -> 0.33 -> 0.10 on
    touches 1, 2, 3, 4+. Validation: ``gex-tap-decay.md`` — observed reaction
    rate by touch number vs the claimed schedule.

    On a fresh touch of a node, bias is 'fade' (expect the node to hold) with
    the touch-count-implied probability; with no touch, bias is 'neutral'.
    """
    out = _base_result("node_tap_decay")
    ctx = context if context is not None else new_context()
    s = _resolve_spot(profile, spot)
    nodes = _node_levels(profile)
    tracked = {n: lvl for n, lvl in nodes.items() if lvl is not None}
    out["levels"] = sorted(set(tracked.values()))
    out["details"]["nodes_tracked"] = tracked
    if not tracked:
        out["rationale"] = "No node levels identifiable — no tap signal (experimental)."
        return out

    tol = float(ctx.get("touch_tol_pct", 0.05)) / 100.0
    touched = None
    best = None
    for name, level in tracked.items():
        dist = abs(s - level) / s
        if dist <= tol and (best is None or dist < best):
            best = dist
            touched = name

    if touched is None:
        ctx["last_touch"] = None
        out["rationale"] = (
            f"Spot {s:.2f} is not touching a tracked node — no tap to score "
            "(experimental; stand by for the next snapshot)."
        )
        return out

    if touched != ctx.get("last_touch"):
        ctx["touches"][touched] = int(ctx["touches"].get(touched, 0)) + 1
    ctx["last_touch"] = touched
    count = int(ctx["touches"][touched])
    prob = PREDICTED_REACTION_PROB.get(count, PREDICTED_REACTION_PROB_4PLUS)

    out["levels"] = [tracked[touched]]
    out["bias"] = "fade"
    out["details"].update({
        "node": touched,
        "node_level": tracked[touched],
        "touch_count": count,
        "predicted_reaction_prob": prob,
        "touches_this_session": dict(ctx["touches"]),
    })
    out["rationale"] = (
        f"EXPERIMENTAL: touch #{count} of the {touched} node at "
        f"{tracked[touched]:.2f}. Published claim under test: reaction "
        f"probability {prob:.0%} (schedule 80/66/33/10% for touches 1/2/3/4+). "
        "Bias: fade the touch — unvalidated, see gex-tap-decay.md."
    )
    return out


def gamma_regime(profile: Any, spot: Optional[float] = None,
                 context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Gamma-regime day-type forecast from the profile's regime read.

    EXPERIMENTAL — hypothesis only. Maps the profile regime to a day-type
    forecast: positive gamma -> range day (fade edges), negative gamma ->
    trend day (momentum), mixed gamma -> whipsaw (fade extremes or stand
    aside). Validation: ``gex-regime.md`` — do positive-gamma days actually
    print smaller ranges than negative-gamma days?

    Returns levels=[put_wall, call_wall] as the expected range bounds,
    bias in {'range', 'trend', 'whipsaw', 'unknown'}.
    """
    out = _base_result("gamma_regime")
    _resolve_spot(profile, spot)  # validates spot availability
    regime_raw = getattr(profile, "regime", None)
    regime = str(regime_raw).strip().lower() if regime_raw is not None else "unknown"
    walls = [w for w in (_as_float(getattr(profile, "put_wall", None)),
                         _as_float(getattr(profile, "call_wall", None)))
             if w is not None]
    out["levels"] = sorted(walls)
    out["details"]["regime"] = regime

    forecast = REGIME_FORECAST.get(regime)
    if forecast is None:
        out["bias"] = "unknown"
        out["rationale"] = (
            f"Regime '{regime_raw}' is not one of positive/negative/mixed — "
            "no day-type forecast (experimental)."
        )
        return out
    bias, explanation = forecast
    out["bias"] = bias
    out["rationale"] = (
        f"EXPERIMENTAL: {explanation} Range bounds from walls: "
        f"{walls if walls else 'unavailable'}. Unvalidated — see "
        "backtests/reports/gex-regime.md."
    )
    return out


def run_all(profile: Any, spot: Optional[float] = None,
            context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Run all three candidate signals on one profile snapshot."""
    return [
        king_magnet(profile, spot, context),
        node_tap_decay(profile, spot, context),
        gamma_regime(profile, spot, context),
    ]
