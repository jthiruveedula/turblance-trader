"""Instinct score: a composite read of dealer structure + flow + vol regime.

Clean-room original. The score is a transparent arithmetic combination of
five named components — it is shown with its parts so a user can see exactly
what drives it. The *interpretation* (bullish/bearish labels) is EXPERIMENTAL
and UNVALIDATED until backtested on real history.

Conventions (fixed, documented, testable):
  r1 (regime)      = +1 if spot is above the zero-gamma flip, else -1.
                     Flip missing -> fall back to the gamma-regime sign.
  r2 (walls)       = +1 if spot within 0.5% of the put wall (support),
                     -1 if within 0.5% of the call wall (resistance), else 0.
  pinned           = spot within 0.3% of the King node -> conviction halved
                     (a pin reads as chop, not trend).
  r3 (flow)        = sign of breadth-weighted sweep directions, capped to
                     +-1; 0 when there are no sweeps. Sweeps are PROXIES.
  r4 (vex)         = 0.5 * vol-regime sign (+1 positive, -1 negative, 0 mixed).

  score = clamp(round(100 * (r1 + r2 + r3 + r4) / 3.5), -100, 100)
"""

from __future__ import annotations

import math


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def normalize_regime(regime) -> int:
    """Map a regime read to +1 / -1 / 0. Accepts strings or numbers."""
    if isinstance(regime, (int, float)) and not isinstance(regime, bool):
        if regime > 0:
            return 1
        if regime < 0:
            return -1
        return 0
    if isinstance(regime, str):
        r = regime.strip().lower()
        if r in ("positive", "pos", "bullish", "+"):
            return 1
        if r in ("negative", "neg", "bearish", "-"):
            return -1
    return 0


def label_instinct(score: int) -> str:
    """Band label for an instinct score. Pure, testable."""
    if score >= 40:
        return "BULLISH"
    if score >= 15:
        return "LEAN BULLISH"
    if score > -15:
        return "NEUTRAL"
    if score > -40:
        return "LEAN BEARISH"
    return "BEARISH"


def instinct_score(
    S,
    F,
    CW,
    PW,
    K,
    R,
    sweeps,
    V,
) -> dict:
    """Composite instinct score from dealer structure + flow + vol.

    Parameters
    ----------
    S : float — spot price of the underlying.
    F : float | None — zero-gamma flip strike (None = no crossing found).
    CW : float | None — call wall strike (resistance).
    PW : float | None — put wall strike (support).
    K : float | None — King-node strike (max |exposure|).
    R : gamma-regime read ('positive' | 'negative' | 'mixed', or +1/-1/0).
    sweeps : iterable of (d, b) — d in {+1 bullish, -1 bearish}, b = breadth
        (distinct strikes). Breadth is capped at 3 per sweep.
    V : vol-regime read ('positive' | 'negative' | 'mixed', or +1/-1/0).

    Returns a dict with score, label, the four components, pinned flag,
    conviction multiplier, and the experimental marker.
    """
    if S is None or (isinstance(S, float) and math.isnan(S)) or S <= 0:
        raise ValueError("instinct_score requires a positive spot S")

    r = normalize_regime(R)
    v = normalize_regime(V)

    # r1: regime — above the flip is the constructive read.
    if F is not None and not (isinstance(F, float) and math.isnan(F)):
        r1 = 1 if S > F else -1
    else:
        r1 = 1 if r > 0 else -1

    # r2: wall proximity — within 0.5% of a wall.
    r2 = 0
    at_support = at_resistance = False
    if PW is not None and not (isinstance(PW, float) and math.isnan(PW)):
        if abs(S - PW) / S <= 0.005:
            r2 = 1
            at_support = True
    if CW is not None and not (isinstance(CW, float) and math.isnan(CW)):
        if abs(S - CW) / S <= 0.005:
            r2 = -1  # resistance takes precedence over support
            at_resistance = True
            at_support = False

    # pinned: within 0.3% of the King node -> conviction halved.
    pinned = False
    if K is not None and not (isinstance(K, float) and math.isnan(K)):
        pinned = abs(S - K) / S <= 0.003
    conviction = 0.5 if pinned else 1.0

    # r3: sweep flow — breadth-weighted direction, capped to +-1.
    sweeps = list(sweeps or [])
    if sweeps:
        total = sum(d * min(b, 3) for d, b in sweeps)
        r3 = _sign(total)
    else:
        r3 = 0

    # r4: vol regime tailwind/headwind.
    r4 = 0.5 * v

    raw = 100.0 * (r1 + r2 + r3 + r4) / 3.5
    score = max(-100, min(100, int(round(raw))))

    return {
        "score": score,
        "label": label_instinct(score),
        "components": {"r1_regime": r1, "r2_walls": r2, "r3_flow": r3,
                       "r4_vex": r4},
        "pinned": pinned,
        "conviction": conviction,
        "at_support": at_support,
        "at_resistance": at_resistance,
        "experimental": True,
    }
