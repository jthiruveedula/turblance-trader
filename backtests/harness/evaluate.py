"""Score candidate GEX signals on dated profile sequences + price series.

Session schema (list of dicts)::

    {
        'date': 'YYYY-MM-DD',
        'profiles': [(quote_time, profile), ...],  # time-ordered GEX profiles
        'prices': [(timestamp, price), ...],       # intraday series, time-ordered
        'regime': 'positive' | 'negative' | 'mixed' | ...  # day's regime read
    }

Profiles implement the shared GEX-engine contract (see signals.py).

Metrics
-------
(a) king_magnet_hit_rate — fraction of sessions where price touched the King
    node before the close.
(b) tap_decay_observed — observed reaction rate by touch number (1st, 2nd,
    3rd, 4th+) vs the claimed 80/66/33/10% schedule.
(c) regime_range_accuracy — do positive-gamma days actually print smaller
    ranges than negative-gamma days?

Costs hook
----------
Every function accepts ``costs`` (a dict). There is no trade execution in
this harness, so costs do not change any number today; the hook exists so
that when P&L attribution is added, slippage/commission assumptions are an
explicit, documented input rather than a hidden default.

    DEFAULT_COSTS = {
        'slippage_bps': 2.0,            # placeholder; not yet applied to any trade
        'commission_per_contract': 0.0, # placeholder
        'note': 'No trade execution in the current harness. Costs are recorded
                 for transparency and will feed P&L attribution when added.',
    }

STATUS: EXPERIMENTAL — all statistics are UNVALIDATED until computed on
real forward-captured data with an in/out-of-sample split.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from backtests.harness import signals as _signals
from backtests.harness import flow_signals as _flow_signals

DEFAULT_COSTS: Dict[str, Any] = {
    "slippage_bps": 2.0,
    "commission_per_contract": 0.0,
    "note": (
        "No trade execution in the current harness, so costs do not change "
        "any number today. The hook is explicit so that when P&L attribution "
        "is added, slippage/commission assumptions are documented inputs, "
        "not hidden defaults."
    ),
}

# The published reaction-probability claim under test (see signals.py).
PREDICTED_REACTION = {1: 0.80, 2: 0.66, 3: 0.33, "4+": 0.10}


class InsufficientDataError(Exception):
    """Raised when there is not enough data to compute honest statistics."""


def _costs(costs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dict(costs) if costs is not None else dict(DEFAULT_COSTS)


def _check_sessions(sessions: List[Dict[str, Any]], need: str) -> None:
    if not sessions:
        raise InsufficientDataError(
            f"Cannot compute {need}: zero sessions supplied. "
            "Forward capture has not produced data yet."
        )


def _session_prices(session: Dict[str, Any]) -> List[Tuple[Any, float]]:
    prices = [(t, float(p)) for t, p in session.get("prices", [])]
    if len(prices) < 2:
        raise InsufficientDataError(
            f"Session {session.get('date', '?')}: fewer than 2 price points — "
            "cannot score outcomes."
        )
    return prices


def _first_profile(session: Dict[str, Any]) -> Any:
    profiles = session.get("profiles", [])
    if not profiles:
        raise InsufficientDataError(
            f"Session {session.get('date', '?')}: no GEX profiles."
        )
    return profiles[0][1]


def _bucket_key(touch_count: int) -> Any:
    return touch_count if touch_count <= 3 else "4+"


def _median(values) -> float:
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def wilson_interval(hits: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for a binomial rate; (None, None) if n == 0."""
    if n <= 0:
        return None, None
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def king_magnet_hit_rate(
    sessions: List[Dict[str, Any]],
    tol_pct: float = 0.05,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(a) Fraction of sessions where price touched the King node before close.

    A session counts as a hit when any intraday price print comes within
    ``tol_pct`` percent of the session's King-node strike (taken from the
    first profile of the day).
    """
    _check_sessions(sessions, "king-magnet hit rate")
    tol = tol_pct / 100.0
    hits = 0
    details = []
    for session in sessions:
        king = _signals._king_strike(_first_profile(session))
        prices = _session_prices(session)
        if king is None:
            details.append({"date": session.get("date"), "hit": None,
                            "note": "no King node identifiable"})
            continue
        hit = any(abs(p - king) / king <= tol for _, p in prices)
        hits += hit
        details.append({"date": session.get("date"), "hit": bool(hit),
                        "king": king})
    scored = [d for d in details if d["hit"] is not None]
    n = len(scored)
    rate = hits / n if n else None
    lo, hi = wilson_interval(hits, n)
    return {
        "metric": "king_magnet_hit_rate",
        "n_sessions": len(sessions),
        "n_scored": n,
        "hits": hits,
        "hit_rate": rate,
        "wilson_95ci": [lo, hi],
        "tol_pct": tol_pct,
        "costs": _costs(costs),
        "per_session": details,
        "status": "UNVALIDATED — EXPERIMENTAL" if n == 0 else "computed; unvalidated until in/out-of-sample on real data",
    }


def tap_decay_observed(
    sessions: List[Dict[str, Any]],
    tol_pct: float = 0.05,
    breakthrough_tol_pct: float = 0.10,
    window_minutes: int = 30,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(b) Observed reaction rate by touch number vs the 80/66/33/10% claim.

    Touch detection: walk the session's intraday prices in time order; a new
    touch is recorded when price comes within ``tol_pct``% of a tracked node
    (King / call wall / put wall, from the day's first profile) after having
    been away from that node.

    Reaction definition (documented, debatable — see limitations): in the
    window after a touch (until the next touch or ``window_minutes``, whichever
    is first), the node "held" (reaction) if price moved away from the level
    by at least ``breakthrough_tol_pct``% without first crossing through it
    by that amount. Crossing through = breakthrough = no reaction. A touch
    with no price prints in its window is "undetermined" and excluded from
    the buckets.
    """
    _check_sessions(sessions, "tap-decay observation")
    tol = tol_pct / 100.0
    thr = breakthrough_tol_pct / 100.0
    buckets: Dict[Any, Dict[str, int]] = {
        k: {"touches": 0, "reactions": 0, "undetermined": 0}
        for k in (1, 2, 3, "4+")
    }

    for session in sessions:
        nodes = {n: lvl for n, lvl in
                 _signals._node_levels(_first_profile(session)).items()
                 if lvl is not None}
        if not nodes:
            continue
        prices = _session_prices(session)
        # --- detect touch events in time order ---
        events: List[Tuple[Any, str, float, float]] = []  # (t, node, level, spot)
        last_touched: Optional[str] = None
        for t, p in prices:
            nearest, best = None, None
            for name, level in nodes.items():
                dist = abs(p - level) / p
                if dist <= tol and (best is None or dist < best):
                    best, nearest = dist, name
            if nearest is not None and nearest != last_touched:
                events.append((t, nearest, nodes[nearest], p))
            last_touched = nearest
        # --- score each touch ---
        touch_counts: Dict[str, int] = {}
        for i, (t, node, level, spot_at) in enumerate(events):
            touch_counts[node] = touch_counts.get(node, 0) + 1
            bucket = _bucket_key(touch_counts[node])
            t_end = t + timedelta(minutes=window_minutes)
            if i + 1 < len(events):
                t_end = min(t_end, events[i + 1][0])
            window = [p for ts, p in prices if t < ts <= t_end]
            if not window:
                buckets[bucket]["undetermined"] += 1
                continue
            side = 1 if spot_at >= level else -1
            # side=+1 (touched from above): reaction = bounce back up,
            #   breakthrough = fall through downward.
            # side=-1 (touched from below): reaction = rejection downward,
            #   breakthrough = push through upward.
            away = max((p - level) * side for p in window) / level
            through = max((level - p) * side for p in window) / level
            buckets[bucket]["touches"] += 1
            if through >= thr:
                pass  # breakthrough: level failed, no reaction
            elif away >= thr:
                buckets[bucket]["reactions"] += 1
            # else: touched and drifted — counts as a touch, not a reaction

    result_buckets = {}
    for key in (1, 2, 3, "4+"):
        b = buckets[key]
        n = b["touches"]
        rate = b["reactions"] / n if n else None
        lo, hi = wilson_interval(b["reactions"], n)
        result_buckets[key] = {
            "touches": n,
            "reactions": b["reactions"],
            "undetermined": b["undetermined"],
            "observed_rate": rate,
            "wilson_95ci": [lo, hi],
            "predicted_rate": PREDICTED_REACTION[key],
        }
    return {
        "metric": "tap_decay_observed",
        "n_sessions": len(sessions),
        "buckets": result_buckets,
        "predicted_schedule": {str(k): v for k, v in PREDICTED_REACTION.items()},
        "definitions": {
            "touch_tol_pct": tol_pct,
            "breakthrough_tol_pct": breakthrough_tol_pct,
            "window_minutes": window_minutes,
            "reaction": "price moved away from the node by >= breakthrough_tol_pct% "
                        "without crossing through it by that amount",
        },
        "costs": _costs(costs),
        "status": "UNVALIDATED — EXPERIMENTAL; definitions are debatable, see limitations",
    }


def regime_range_accuracy(
    sessions: List[Dict[str, Any]],
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(c) Did positive-gamma days actually print smaller ranges than negative-gamma days?

    Daily range = (high - low) / first price of the session. Forecast rule
    under test: a positive-gamma day is "correct" when its range is below the
    all-session median range; a negative-gamma day is "correct" when its
    range is above it. Mixed-regime days are reported, not scored.
    """
    _check_sessions(sessions, "regime range accuracy")
    scored = []
    mixed = []
    for session in sessions:
        prices = _session_prices(session)
        vals = [p for _, p in prices]
        rng = (max(vals) - min(vals)) / vals[0] * 100.0
        regime = str(session.get("regime", "unknown")).strip().lower()
        entry = {"date": session.get("date"), "regime": regime, "range_pct": rng}
        if regime in ("positive", "negative"):
            scored.append(entry)
        else:
            mixed.append(entry)
    if not scored:
        raise InsufficientDataError(
            "No positive/negative-regime sessions to score."
        )
    median_range = _median(e["range_pct"] for e in scored)
    correct = 0
    pos_ranges, neg_ranges = [], []
    for e in scored:
        if e["regime"] == "positive":
            pos_ranges.append(e["range_pct"])
            ok = e["range_pct"] < median_range
        else:
            neg_ranges.append(e["range_pct"])
            ok = e["range_pct"] > median_range
        e["forecast_correct"] = bool(ok)
        correct += ok
    n = len(scored)
    accuracy = correct / n
    lo, hi = wilson_interval(correct, n)
    mean_pos = sum(pos_ranges) / len(pos_ranges) if pos_ranges else None
    mean_neg = sum(neg_ranges) / len(neg_ranges) if neg_ranges else None
    return {
        "metric": "regime_range_accuracy",
        "n_sessions": len(sessions),
        "n_scored": n,
        "n_mixed_unscored": len(mixed),
        "accuracy": accuracy,
        "wilson_95ci": [lo, hi],
        "median_range_pct": median_range,
        "mean_range_pct_positive": mean_pos,
        "mean_range_pct_negative": mean_neg,
        "direction_match": (mean_pos < mean_neg
                            if mean_pos is not None and mean_neg is not None
                            else None),
        "costs": _costs(costs),
        "per_session": scored,
        "status": "UNVALIDATED — EXPERIMENTAL",
    }


def run_all(
    sessions: List[Dict[str, Any]],
    tol_pct: float = 0.05,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run all three validation metrics over a session list."""
    _check_sessions(sessions, "full validation")
    return {
        "n_sessions": len(sessions),
        "king_magnet": king_magnet_hit_rate(sessions, tol_pct=tol_pct, costs=costs),
        "tap_decay": tap_decay_observed(sessions, tol_pct=tol_pct, costs=costs),
        "regime": regime_range_accuracy(sessions, costs=costs),
        "costs": _costs(costs),
        "status": "UNVALIDATED — EXPERIMENTAL: no real data yet",
    }


# ---------------------------------------------------------------------------
# Flow / VEX metrics (session schema extension)
#
# Sessions for these metrics carry, in addition to the base schema, either:
#   'flow_profile' — a flow profile (see flow_signals.py; duck-typed), or
#   'vex_profile'  — a VEX profile (see flow_signals.py; duck-typed).
# When the key is missing the metric falls back to the first entry of
# 'profiles' only if it duck-types as the right profile (has
# 'unusual_strikes' for flow, 'vol_regime' for VEX); otherwise the session
# is reported as unscored, never guessed at.
#
# Forward window: a signal on session i is scored against the move from
# session i's close to the close N sessions ahead
# (``n_sessions_ahead``). Sessions without N sessions of follow-up are
# unscored (reported, not silently dropped into the denominator).
# ---------------------------------------------------------------------------


def _order_sessions(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(sessions, key=lambda s: str(s.get("date", "")))


def _last_price(session: Dict[str, Any]) -> float:
    prices = session.get("prices", [])
    if not prices:
        raise InsufficientDataError(
            f"Session {session.get('date', '?')}: no price points — "
            "cannot form a session close."
        )
    return float(prices[-1][1])


def _forward_returns(
    ordered: List[Dict[str, Any]], n_sessions_ahead: int
) -> Dict[str, float]:
    """Map session date -> % move from its close to the close N sessions ahead."""
    closes = {}
    for s in ordered:
        try:
            closes[str(s.get("date"))] = _last_price(s)
        except InsufficientDataError:
            continue
    out: Dict[str, float] = {}
    for i, s in enumerate(ordered):
        key = str(s.get("date"))
        if i + n_sessions_ahead >= len(ordered) or key not in closes:
            continue
        later = ordered[i + n_sessions_ahead]
        later_key = str(later.get("date"))
        if later_key not in closes or closes[key] == 0:
            continue
        out[key] = (closes[later_key] - closes[key]) / closes[key] * 100.0
    return out


def _forward_realized_vol(
    ordered: List[Dict[str, Any]], i: int, n_sessions_ahead: int
) -> Optional[float]:
    """Realized volatility (% std of close-to-close returns) over the N
    sessions ahead of session i. Documented IV PROXY: true IV validation
    needs an ATM-IV series (planned via IBKR); until then this is what the
    vex-regime hypothesis can be measured against. None when any close in
    the window is missing."""
    closes = []
    for j in range(i, i + n_sessions_ahead + 1):
        if j >= len(ordered):
            return None
        try:
            closes.append(_last_price(ordered[j]))
        except InsufficientDataError:
            return None
    rets = [(b - a) / a for a, b in zip(closes, closes[1:]) if a]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * 100.0


def _flow_profile(session: Dict[str, Any]) -> Optional[Any]:
    prof = session.get("flow_profile")
    if prof is not None:
        return prof
    profiles = session.get("profiles", [])
    if profiles and hasattr(profiles[0][1], "unusual_strikes"):
        return profiles[0][1]
    return None


def _vex_profile(session: Dict[str, Any]) -> Optional[Any]:
    prof = session.get("vex_profile")
    if prof is not None:
        return prof
    profiles = session.get("profiles", [])
    if profiles and hasattr(profiles[0][1], "vol_regime"):
        return profiles[0][1]
    return None


def _split_in_out(
    sessions: List[Dict[str, Any]], in_sample_frac: float
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Chronological in/out-of-sample split matching the report convention:
    first ``in_sample_frac`` of sessions are in-sample, the rest are
    out-of-sample (the only numbers that count toward validation)."""
    n = len(sessions)
    cut = int(n * in_sample_frac)
    if n >= 2:
        cut = max(1, min(n - 1, cut))
    return sessions[:cut], sessions[cut:]


def _unusual_burst_core(
    sessions: List[Dict[str, Any]],
    fwd: Dict[str, float],
    n_sessions_ahead: int,
    costs: Dict[str, Any],
) -> Dict[str, Any]:
    scored: List[Dict[str, Any]] = []
    n_neutral = 0
    n_no_forward = 0
    n_no_profile = 0
    for s in sessions:
        prof = _flow_profile(s)
        date = str(s.get("date"))
        if prof is None:
            n_no_profile += 1
            continue
        sig = _flow_signals.unusual_volume_burst(prof)
        bias = sig["bias"]
        if bias == "neutral":
            n_neutral += 1
            continue
        if date not in fwd:
            n_no_forward += 1
            continue
        ret = fwd[date]
        hit = (ret > 0 and bias == "bullish") or (ret < 0 and bias == "bearish")
        scored.append({
            "date": s.get("date"),
            "bias": bias,
            f"fwd_return_pct_{n_sessions_ahead}sess": round(ret, 4),
            "hit": bool(hit),
            "n_unusual_strikes": len(getattr(prof, "unusual_strikes", []) or []),
        })
    hits = sum(1 for e in scored if e["hit"])
    n = len(scored)
    rate = hits / n if n else None
    lo, hi = wilson_interval(hits, n)
    return {
        "metric": "unusual_burst_hit_rate",
        "n_sessions": len(sessions),
        "n_scored": n,
        "n_neutral_unscored": n_neutral,
        "n_no_forward_window": n_no_forward,
        "n_no_flow_profile": n_no_profile,
        "hits": hits,
        "hit_rate": rate,
        "wilson_95ci": [lo, hi],
        "n_sessions_ahead": n_sessions_ahead,
        "costs": dict(costs),
        "per_session": scored,
        "status": "UNVALIDATED — EXPERIMENTAL" if n else "no directional bursts to score",
    }


def unusual_burst_hit_rate(
    sessions: List[Dict[str, Any]],
    n_sessions_ahead: int = 5,
    in_sample_frac: float = 0.6,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(d) Unusual-volume burst hit rate: did price move in the signal's
    direction within the next ``n_sessions_ahead`` sessions?

    A session is a hit when ``unusual_volume_burst`` is directional
    ('bullish'/'bearish') and the sign of the forward N-session close move
    matches. Neutral signals, sessions without a flow profile, and sessions
    without a full forward window are reported but never scored — they do
    not enter the denominator.
    """
    _check_sessions(sessions, "unusual-burst hit rate")
    if n_sessions_ahead < 1:
        raise ValueError("n_sessions_ahead must be >= 1.")
    ordered = _order_sessions(sessions)
    fwd = _forward_returns(ordered, n_sessions_ahead)
    c = _costs(costs)
    result = _unusual_burst_core(ordered, fwd, n_sessions_ahead, c)
    if result["n_scored"] == 0:
        raise InsufficientDataError(
            "Cannot compute unusual-burst hit rate: no directional bursts "
            "with a full forward window. Cold-start baselines mean early "
            "snapshots produce neutral signals by design."
        )
    in_s, out_s = _split_in_out(ordered, in_sample_frac)
    result["in_sample"] = _unusual_burst_core(in_s, fwd, n_sessions_ahead, c)
    result["out_of_sample"] = _unusual_burst_core(out_s, fwd, n_sessions_ahead, c)
    result["in_sample_frac"] = in_sample_frac
    result["status"] = "computed; UNVALIDATED until in/out-of-sample on real data"
    return result


def _sweep_followthrough_core(
    sessions: List[Dict[str, Any]],
    fwd: Dict[str, float],
    n_sessions_ahead: int,
    costs: Dict[str, Any],
) -> Dict[str, Any]:
    scored: List[Dict[str, Any]] = []
    n_neutral = 0
    n_no_forward = 0
    n_no_profile = 0
    for s in sessions:
        prof = _flow_profile(s)
        date = str(s.get("date"))
        if prof is None:
            n_no_profile += 1
            continue
        sig = _flow_signals.sweep_followthrough(prof)
        bias = sig["bias"]
        if bias == "neutral":
            n_neutral += 1
            continue
        if date not in fwd:
            n_no_forward += 1
            continue
        ret = fwd[date]
        hit = (ret > 0 and bias == "bullish") or (ret < 0 and bias == "bearish")
        scored.append({
            "date": s.get("date"),
            "bias": bias,
            f"fwd_return_pct_{n_sessions_ahead}sess": round(ret, 4),
            "hit": bool(hit),
            "event_score": sig["details"].get("event_score"),
            "event_proxy": sig["details"].get("proxy"),
        })
    hits = sum(1 for e in scored if e["hit"])
    n = len(scored)
    rate = hits / n if n else None
    lo, hi = wilson_interval(hits, n)
    return {
        "metric": "sweep_followthrough_rate",
        "n_sessions": len(sessions),
        "n_scored": n,
        "n_neutral_unscored": n_neutral,
        "n_no_forward_window": n_no_forward,
        "n_no_flow_profile": n_no_profile,
        "hits": hits,
        "followthrough_rate": rate,
        "wilson_95ci": [lo, hi],
        "n_sessions_ahead": n_sessions_ahead,
        "costs": dict(costs),
        "per_session": scored,
        "status": (
            "UNVALIDATED — EXPERIMENTAL; every scored event is an "
            "UNCONFIRMED sweep proxy, not a confirmed sweep"
            if n else "no sweep-like events to score"
        ),
    }


def sweep_followthrough_rate(
    sessions: List[Dict[str, Any]],
    n_sessions_ahead: int = 5,
    in_sample_frac: float = 0.6,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(e) Sweep follow-through rate: did price move in the hypothesized
    direction within the next ``n_sessions_ahead`` sessions after a
    sweep-like event?

    HONEST CAVEAT: this scores UNCONFIRMED PROXIES, not confirmed sweeps.
    A high rate here would only justify building the IBKR tick
    confirmation path, not trading the proxies. Neutral days (no events)
    and days without a full forward window are reported, not scored.
    """
    _check_sessions(sessions, "sweep follow-through rate")
    if n_sessions_ahead < 1:
        raise ValueError("n_sessions_ahead must be >= 1.")
    ordered = _order_sessions(sessions)
    fwd = _forward_returns(ordered, n_sessions_ahead)
    c = _costs(costs)
    result = _sweep_followthrough_core(ordered, fwd, n_sessions_ahead, c)
    if result["n_scored"] == 0:
        raise InsufficientDataError(
            "Cannot compute sweep follow-through rate: no directional "
            "sweep-like events with a full forward window."
        )
    in_s, out_s = _split_in_out(ordered, in_sample_frac)
    result["in_sample"] = _sweep_followthrough_core(in_s, fwd, n_sessions_ahead, c)
    result["out_of_sample"] = _sweep_followthrough_core(out_s, fwd, n_sessions_ahead, c)
    result["in_sample_frac"] = in_sample_frac
    result["status"] = (
        "computed; UNVALIDATED until in/out-of-sample on real data; "
        "scored events remain unconfirmed proxies"
    )
    return result


def _vex_iv_core(
    sessions: List[Dict[str, Any]],
    ordered: List[Dict[str, Any]],
    n_sessions_ahead: int,
    costs: Dict[str, Any],
) -> Dict[str, Any]:
    scored: List[Dict[str, Any]] = []
    mixed: List[Dict[str, Any]] = []
    n_no_profile = 0
    n_no_forward = 0
    index_of = {id(s): i for i, s in enumerate(ordered)}
    for s in sessions:
        prof = _vex_profile(s)
        date = s.get("date")
        if prof is None:
            n_no_profile += 1
            continue
        regime = str(getattr(prof, "vol_regime", "unknown")).strip().lower()
        rv = _forward_realized_vol(ordered, index_of[id(s)], n_sessions_ahead)
        entry = {"date": date, "vol_regime": regime,
                 f"fwd_realized_vol_pct_{n_sessions_ahead}sess": rv}
        if regime in ("positive", "negative"):
            if rv is None:
                n_no_forward += 1
                continue
            scored.append(entry)
        else:
            mixed.append(entry)
    if scored:
        median_rv = _median(e[f"fwd_realized_vol_pct_{n_sessions_ahead}sess"]
                            for e in scored)
    else:
        median_rv = None
    correct = 0
    pos_rvs, neg_rvs = [], []
    for e in scored:
        rv = e[f"fwd_realized_vol_pct_{n_sessions_ahead}sess"]
        if e["vol_regime"] == "positive":
            pos_rvs.append(rv)
            ok = rv < median_rv  # contraction: realized vol below median
        else:
            neg_rvs.append(rv)
            ok = rv > median_rv  # expansion: realized vol above median
        e["forecast_correct"] = bool(ok)
        correct += ok
    n = len(scored)
    accuracy = correct / n if n else None
    lo, hi = wilson_interval(correct, n)
    mean_pos = sum(pos_rvs) / len(pos_rvs) if pos_rvs else None
    mean_neg = sum(neg_rvs) / len(neg_rvs) if neg_rvs else None
    return {
        "metric": "vex_regime_iv_accuracy",
        "n_sessions": len(sessions),
        "n_scored": n,
        "n_mixed_unscored": len(mixed),
        "n_no_forward_window": n_no_forward,
        "n_no_vex_profile": n_no_profile,
        "accuracy": accuracy,
        "wilson_95ci": [lo, hi],
        "median_fwd_realized_vol_pct": median_rv,
        "mean_fwd_rv_positive": mean_pos,
        "mean_fwd_rv_negative": mean_neg,
        "direction_match": (mean_pos < mean_neg
                            if mean_pos is not None and mean_neg is not None
                            else None),
        "iv_proxy": (
            "forward realized close-to-close volatility over "
            f"{n_sessions_ahead} sessions — a PROXY, not implied volatility. "
            "True IV validation needs an ATM-IV series (IBKR path)."
        ),
        "n_sessions_ahead": n_sessions_ahead,
        "costs": dict(costs),
        "per_session": scored,
        "status": (
            "UNVALIDATED — EXPERIMENTAL; regime cutoffs are arbitrary "
            "heuristics"
            if n else "no positive/negative-regime sessions to score"
        ),
    }


def vex_regime_iv_accuracy(
    sessions: List[Dict[str, Any]],
    n_sessions_ahead: int = 5,
    in_sample_frac: float = 0.6,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """(f) VEX-regime IV accuracy: does the vol-regime forecast match forward
    realized volatility?

    Forecast rule under test (EXPERIMENTAL): a positive-regime day forecasts
    IV *contraction* (scored correct when forward realized vol is below the
    scored-session median); a negative-regime day forecasts IV *expansion*
    (correct when above). Mixed-regime days are reported, not scored.

    HONEST LIMITATIONS: (1) the regime cutoffs (+/-0.5) are arbitrary
    heuristics; (2) forward realized volatility is an IV proxy — the real
    test needs ATM-IV prints. A pass here would only justify building the
    IV series, not acting on the regime read.
    """
    _check_sessions(sessions, "vex-regime IV accuracy")
    if n_sessions_ahead < 1:
        raise ValueError("n_sessions_ahead must be >= 1.")
    ordered = _order_sessions(sessions)
    c = _costs(costs)
    result = _vex_iv_core(ordered, ordered, n_sessions_ahead, c)
    if result["n_scored"] == 0:
        raise InsufficientDataError(
            "Cannot compute vex-regime IV accuracy: no positive/negative "
            "regime sessions with a full forward window."
        )
    in_s, out_s = _split_in_out(ordered, in_sample_frac)
    result["in_sample"] = _vex_iv_core(in_s, ordered, n_sessions_ahead, c)
    result["out_of_sample"] = _vex_iv_core(out_s, ordered, n_sessions_ahead, c)
    result["in_sample_frac"] = in_sample_frac
    result["status"] = "computed; UNVALIDATED until in/out-of-sample on real data"
    return result


def run_all_flow(
    sessions: List[Dict[str, Any]],
    n_sessions_ahead: int = 5,
    in_sample_frac: float = 0.6,
    costs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run all three flow/VEX validation metrics over a session list."""
    _check_sessions(sessions, "flow/vex validation")
    return {
        "n_sessions": len(sessions),
        "n_sessions_ahead": n_sessions_ahead,
        "unusual_burst": unusual_burst_hit_rate(
            sessions, n_sessions_ahead=n_sessions_ahead,
            in_sample_frac=in_sample_frac, costs=costs),
        "sweep_followthrough": sweep_followthrough_rate(
            sessions, n_sessions_ahead=n_sessions_ahead,
            in_sample_frac=in_sample_frac, costs=costs),
        "vex_regime": vex_regime_iv_accuracy(
            sessions, n_sessions_ahead=n_sessions_ahead,
            in_sample_frac=in_sample_frac, costs=costs),
        "costs": _costs(costs),
        "status": "UNVALIDATED — EXPERIMENTAL: no real data yet",
    }
