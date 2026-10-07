"""Sweep-like detection from chain snapshots WITHOUT tick data.

Clean-room implementation: the proxies below are built only from public
options-market microstructure knowledge (trade location vs the bid/ask
midpoint, multi-strike volume bursts). No proprietary data,
implementations, or branding are used or copied.

WHAT IS DETECTED (mechanical)
-----------------------------
A "sweep-like" cluster is a group of contracts on the SAME snapshot that
share (symbol, expiry, option_type, aggression side) and satisfy:

1. Trade-location proxy: ``last`` prints at/near the offer
   (``(last - bid) / (ask - bid) >= lift_threshold``, default 0.75) for
   "lifted" flow, or at/near the bid (``<= hit_threshold``, default 0.25)
   for "hit" flow. Trades near the midpoint are never sweep legs.
2. Volume burst: the contract's ``volume_z`` (from
   ``unusual_activity.score_unusual_activity``) meets ``volume_z_min``
   (default 2.0). Without baselines, an absolute ``burst_volume_min``
   fallback applies — cruder, and flagged as such.
3. Breadth: at least ``min_strikes`` (default 3) DISTINCT strikes qualify.

Each cluster is scored and ranked; the score is a heuristic strength
measure, documented below.

PROXY DISCLAIMER — READ THIS
----------------------------
These are PROXIES, not confirmed sweeps. A real sweep is a single order
(or linked orders) executed across multiple strikes/exchanges in a short
time window, and confirming one requires tick-level trade data (time,
price, size, exchange per print) that a chain snapshot does not contain.
What this module sees — aggressive prints plus elevated volume on several
strikes of one expiry — is *consistent with* sweep activity but can also
be independent orders arriving in the same snapshot window. Every sweep
dict therefore carries ``"proxy": True`` and ``"experimental": True``.
Real confirmation needs tick data (planned via the IBKR path later); do
not trade on these outputs.

MULTI-EXPIRY ROLLUP
-------------------
A sweep that spans weeklies and monthlies should read as ONE event. After
per-expiry clusters are found, clusters sharing (symbol, option_type,
side) whose strike ranges overlap are merged into a single event. The
event keeps an anchor ``expiry`` (the expiry contributing the most
volume), plus ``expiries`` (all expiries in the event) and
``strikes_hit_by_expiry``. Merging strike ranges is a heuristic — two
independent same-direction bursts on adjacent expiries will be merged.
That trade-off is explicit: breadth across the term structure is treated
as one institutional-sized footprint until tick data can split it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..capture.schema import validate_schema
from .unusual_activity import score_unusual_activity

_DEFAULT_LIFT = 0.75
_DEFAULT_HIT = 0.25
_DEFAULT_MIN_STRIKES = 3
_DEFAULT_VOLUME_Z_MIN = 2.0
_DEFAULT_BURST_VOLUME_MIN = 500
_Z_CAP = 6.0


def classify_trade_location(bid, ask, last,
                            lift_threshold=_DEFAULT_LIFT,
                            hit_threshold=_DEFAULT_HIT):
    """Classify where ``last`` printed inside the bid/ask spread.

    Returns "lifted" when the print is at/near the offer
    (``(last-bid)/(ask-bid) >= lift_threshold``), "hit" when at/near the
    bid (``<= hit_threshold``), "mid" otherwise, and None when the spread
    is unusable (NaN bid/ask/last, or ask <= bid). This is a LOCATION
    proxy only: a lifted print suggests aggressive buying, a hit print
    aggressive selling — who initiated and why is NOT observed.
    """
    try:
        bid, ask, last = float(bid), float(ask), float(last)
    except (TypeError, ValueError):
        return None
    if any(math.isnan(v) for v in (bid, ask, last)):
        return None
    if ask <= bid:
        return None  # crossed/locked or zero-width spread: no information
    loc = (last - bid) / (ask - bid)
    if loc >= lift_threshold:
        return "lifted"
    if loc <= hit_threshold:
        return "hit"
    return "mid"


def _leg_strength(row, volume_z_min, burst_volume_min):
    """Decide whether a contract row qualifies as a sweep leg.

    Returns (qualifies, clipped_z). Prefers the baseline-based volume_z;
    without a usable z it falls back to the absolute ``burst_volume_min``
    threshold (cruder — flagged by the caller via ``used_burst_fallback``).
    """
    volume = row.get("volume")
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return False, 0.0
    if math.isnan(volume) or volume <= 0:
        return False, 0.0
    vz = row.get("volume_z")
    try:
        vz = float(vz)
    except (TypeError, ValueError):
        vz = float("nan")
    if not math.isnan(vz):
        return vz >= volume_z_min, max(0.0, min(vz, _Z_CAP))
    return volume >= burst_volume_min, 0.0


def _score_cluster(legs):
    """Heuristic sweep strength: breadth x average clipped leg strength.

    score = n_distinct_strikes * mean(clipped volume_z of legs).
    Breadth (strikes, not contracts) is what makes a sweep sweep-like, so
    it enters multiplicatively; average z keeps one monster print from
    dominating. EXPERIMENTAL/UNVALIDATED weighting — the score ranks
    candidates within a snapshot, it is not a probability.
    """
    if not legs:
        return 0.0
    strikes = {leg["strike"] for leg in legs}
    avg_z = float(np.mean([leg["leg_z"] for leg in legs])) if legs else 0.0
    return float(len(strikes) * avg_z)


def _ranges_overlap(a_lo, a_hi, b_lo, b_hi):
    return a_lo <= b_hi and b_lo <= a_hi


def _merge_clusters(clusters):
    """Merge per-expiry clusters into multi-expiry events.

    Clusters sharing (symbol, option_type, side) whose strike ranges
    overlap are merged into ONE event (the multi-expiry rollup): a sweep
    across weeklies + monthlies reads as one footprint. The merged
    event's anchor ``expiry`` is the expiry contributing the most volume;
    every expiry is preserved in ``expiries`` and
    ``strikes_hit_by_expiry``.
    """
    if not clusters:
        return []
    # Group by (symbol, option_type, side); merge overlapping strike
    # ranges within each group via a simple sweep-line union.
    grouped = {}
    for c in clusters:
        grouped.setdefault(
            (c["symbol"], c["option_type"], c["side"]), []
        ).append(c)

    merged = []
    for key, items in grouped.items():
        items = sorted(items, key=lambda c: c["strike_lo"])
        cur = None
        for c in items:
            if cur is None:
                cur = c
                continue
            if _ranges_overlap(cur["strike_lo"], cur["strike_hi"],
                               c["strike_lo"], c["strike_hi"]):
                cur = _combine_two(cur, c)
            else:
                merged.append(cur)
                cur = c
        if cur is not None:
            merged.append(cur)
    return merged


def _combine_two(a, b):
    """Combine two overlapping clusters into one event dict."""
    legs = a["legs"] + b["legs"]
    strikes_by_expiry = {}
    for d in (a["strikes_hit_by_expiry"], b["strikes_hit_by_expiry"]):
        for exp, strikes in d.items():
            strikes_by_expiry.setdefault(exp, set()).update(strikes)
    strikes_by_expiry = {
        exp: sorted(s) for exp, s in strikes_by_expiry.items()
    }
    expiries = sorted(strikes_by_expiry)
    # Anchor expiry: the one contributing the most leg volume.
    vol_by_expiry = {}
    for leg in legs:
        vol_by_expiry[leg["expiry"]] = vol_by_expiry.get(
            leg["expiry"], 0.0) + leg["volume"]
    anchor = max(vol_by_expiry, key=lambda e: vol_by_expiry[e])
    all_strikes = sorted({leg["strike"] for leg in legs})
    return {
        "symbol": a["symbol"],
        "option_type": a["option_type"],
        "side": a["side"],
        "expiries": expiries,
        "strikes_hit_by_expiry": strikes_by_expiry,
        "legs": legs,
        "strike_lo": all_strikes[0],
        "strike_hi": all_strikes[-1],
        "total_volume": float(sum(leg["volume"] for leg in legs)),
        "volume_by_expiry": {e: float(v) for e, v in vol_by_expiry.items()},
        "anchor_expiry": anchor,
        "used_burst_fallback": a["used_burst_fallback"]
        or b["used_burst_fallback"],
    }


def detect_sweeps(df, scored_df=None, baselines=None,
                  min_strikes=_DEFAULT_MIN_STRIKES,
                  volume_z_min=_DEFAULT_VOLUME_Z_MIN,
                  lift_threshold=_DEFAULT_LIFT,
                  hit_threshold=_DEFAULT_HIT,
                  burst_volume_min=_DEFAULT_BURST_VOLUME_MIN):
    """Detect sweep-like clusters from a chain snapshot (no tick data).

    Parameters
    ----------
    df : pandas.DataFrame
        Snapshot in the shared 16-column contract schema.
    scored_df : pandas.DataFrame, optional
        Output of ``unusual_activity.score_unusual_activity`` for ``df``
        (carries ``volume_z``). When None, it is computed internally with
        ``baselines`` (possibly None -> cold start -> absolute-volume
        burst fallback).
    baselines : BaselineSet, optional
        Only used when ``scored_df`` is None.
    min_strikes : int, default 3
        Minimum DISTINCT strikes for a cluster to count as sweep-like.
    volume_z_min : float, default 2.0
        Minimum volume z-score for a leg (baseline path).
    lift_threshold / hit_threshold : float, default 0.75 / 0.25
        Trade-location cutoffs for "lifted" / "hit" prints.
    burst_volume_min : int, default 500
        Absolute session-volume fallback for a leg when no volume_z is
        available (cold start). Crude by design; flagged in output.

    Returns
    -------
    list of dict
        One dict per sweep-like event, ranked by ``score`` descending.
        Each carries the contract keys ``expiry`` (anchor expiry),
        ``direction`` ("calls" | "puts"), ``strikes_hit`` (all strikes),
        ``volume``, ``score``, plus ``expiries``,
        ``strikes_hit_by_expiry``, ``side`` ("lifted" | "hit"),
        ``n_legs``, ``avg_leg_z``, ``used_burst_fallback``,
        ``proxy`` (always True), ``experimental`` (always True), and a
        ``note`` stating these are unconfirmed proxies. See the module
        docstring: PROXIES, not confirmed sweeps — tick data required.
    """
    validate_schema(df)
    if scored_df is None:
        scored_df = score_unusual_activity(df, baselines=baselines)
    if len(scored_df) == 0:
        return []

    records = scored_df.to_dict("records")
    clusters = []
    # Per-expiry, per-direction, per-side candidate clusters.
    groups = {}
    for row in records:
        side = classify_trade_location(
            row.get("bid"), row.get("ask"), row.get("last"),
            lift_threshold=lift_threshold, hit_threshold=hit_threshold,
        )
        if side not in ("lifted", "hit"):
            continue
        qualifies, leg_z = _leg_strength(row, volume_z_min, burst_volume_min)
        if not qualifies:
            continue
        try:
            strike = float(row["strike"])
            volume = float(row["volume"])
        except (TypeError, ValueError):
            continue
        if math.isnan(strike):
            continue
        key = (str(row["symbol"]), str(row["expiry"]),
               str(row["option_type"]), side)
        leg = {
            "strike": strike,
            "expiry": str(row["expiry"]),
            "volume": volume,
            "leg_z": float(leg_z),
            "volume_z": row.get("volume_z"),
            "location": side,
        }
        groups.setdefault(key, []).append(leg)

    for (symbol, expiry, option_type, side), legs in groups.items():
        strikes = sorted({leg["strike"] for leg in legs})
        if len(strikes) < int(min_strikes):
            continue
        used_fallback = all(
            leg["volume_z"] is None
            or (isinstance(leg["volume_z"], float)
                and math.isnan(leg["volume_z"]))
            for leg in legs
        )
        clusters.append({
            "symbol": symbol,
            "option_type": option_type,
            "side": side,
            "expiries": [expiry],
            "strikes_hit_by_expiry": {expiry: strikes},
            "legs": legs,
            "strike_lo": strikes[0],
            "strike_hi": strikes[-1],
            "total_volume": float(sum(leg["volume"] for leg in legs)),
            "volume_by_expiry": {
                expiry: float(sum(leg["volume"] for leg in legs))
            },
            "anchor_expiry": expiry,
            "used_burst_fallback": bool(used_fallback),
        })

    merged = _merge_clusters(clusters)

    events = []
    for c in merged:
        score = _score_cluster(c["legs"])
        strikes_hit = sorted({leg["strike"] for leg in c["legs"]})
        direction = "calls" if c["option_type"] == "call" else "puts"
        events.append({
            # --- profile contract keys (exact names) ---
            "expiry": c["anchor_expiry"],
            "direction": direction,
            "strikes_hit": strikes_hit,
            "volume": c["total_volume"],
            "score": float(score),
            # --- extra detail ---
            "symbol": c["symbol"],
            "option_type": c["option_type"],
            "side": c["side"],
            "expiries": c["expiries"],
            "strikes_hit_by_expiry": c["strikes_hit_by_expiry"],
            "volume_by_expiry": c["volume_by_expiry"],
            "n_legs": len(c["legs"]),
            "n_strikes": len(strikes_hit),
            "avg_leg_z": float(np.mean(
                [leg["leg_z"] for leg in c["legs"]])),
            "used_burst_fallback": c["used_burst_fallback"],
            "proxy": True,
            "experimental": True,
            "note": (
                "PROXY, not a confirmed sweep: aggressive prints plus "
                "elevated volume on multiple strikes of the same "
                "direction/side. Independent orders in one snapshot "
                "window can look identical. Confirmation requires "
                "tick-level trade data (planned via IBKR)."
            ),
        })
    events.sort(key=lambda e: e["score"], reverse=True)
    return events
