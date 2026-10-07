"""Candidate flow / VEX / dark-pool signals for the backtest harness.

Each signal is a pure function of the form::

    signal(profile, spot, context) -> dict(signal, experimental, levels,
                                          bias, rationale, details)

matching the contract style of ``signals.py`` (the GEX signals). Profiles
are duck-typed:

* ``unusual_volume_burst`` / ``sweep_followthrough`` take a **flow profile**
  with ``.by_strike``, ``.by_expiry``, ``.spot``, ``.unusual_strikes``
  (list of dicts: strike, option_type, score, reason, experimental),
  ``.sweeps`` (list of dicts: expiry, direction, strikes_hit, volume,
  score, ...), ``.cold_start`` and ``.to_dict()``. See
  ``src/turblance_trader/flow/profile.py``.
* ``vex_regime`` takes a **VEX profile** with ``.by_strike``
  (strike, call_vex, put_vex, net_vex), ``.by_expiry``, ``.spot``,
  ``.vega_flip``, ``.call_vega_wall``, ``.put_vega_wall``,
  ``.vex_king`` (dict), ``.vol_regime`` ('positive'|'negative'|'mixed'),
  ``.to_dict()``. See ``src/turblance_trader/vex/levels.py``.
* ``darkpool_divergence`` takes an ATS-weekly DataFrame (the
  ``darkpool/schema.py`` contract) plus a weekly price series.

``spot`` falls back to ``profile.spot`` when omitted. ``context`` is a
caller-owned dict for state across snapshots within a session; create with
``new_context()``. Signals hold no global state.

STATUS: EXPERIMENTAL — every signal here is an untested hypothesis until
it passes validation on real forward-captured data (see
``backtests/reports/flow-volume-burst.md``, ``flow-sweep-followthrough.md``,
``vex-regime.md``). Do not trade on these outputs. Do not present them as
reliable.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# Vol-regime -> IV-read forecast map under test. The 'contraction' /
# 'expansion' labels are a MECHANICAL READING of dealer vega hedging, not a
# prediction with evidence behind it: the cutoffs that produce vol_regime
# are arbitrary heuristics (see rationale). EXPERIMENTAL.
VOL_REGIME_FORECAST = {
    "positive": ("contraction", "Positive dealer vega: vol sellers dominate — IV tends to decay."),
    "negative": ("expansion", "Negative dealer vega: dealers buy vol on weakness — IV tends to expand."),
    "mixed": ("uncertain", "Mixed dealer vega: no directional IV read."),
}


def new_context() -> Dict[str, Any]:
    """Fresh per-session caller context for stateful flow signals."""
    return {
        "seen_events": [],   # sweep/unusual events already acted on
        "last_signal": None, # last non-neutral signal name + direction
    }


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


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
        "experimental": True,  # unvalidated until a backtest report passes
        "levels": [],
        "bias": "neutral",
        "rationale": "",
        "details": {},
    }


def _cold_start_note(profile: Any) -> Optional[str]:
    """Cold-start guard: profiles without baselines have no real scores."""
    if bool(getattr(profile, "cold_start", False)):
        return (
            "cold start: no baselines have accrued, so volume/OI z-scores "
            "are NaN and there is nothing to interpret yet"
        )
    return None


def unusual_volume_burst(flow_profile: Any, spot: Optional[float] = None,
                         context: Optional[Dict[str, Any]] = None,
                         top_n: int = 5) -> Dict[str, Any]:
    """Unusual-volume burst: clustered directional unusual strikes -> continuation bias.

    EXPERIMENTAL — hypothesis only. The claim under test: when the top
    ``top_n`` unusual strikes (by score) cluster on one option_type side
    (calls or puts) AND sit directionally (calls above spot = upside flow,
    puts below spot = downside flow), the session's remaining price action
    continues in that direction. Validation:
    ``backtests/reports/flow-volume-burst.md`` — did price move in the
    signal direction within N sessions?

    RATIONALE CAVEAT: baselines are cold-start until history accrues. With
    ``profile.cold_start`` true, ``unusual_strikes`` is EMPTY (scores are
    NaN, never guessed) and this signal returns neutral — by design, not by
    bug. The first honest read requires ~5+ snapshots of history per
    contract (see ``compute_baselines`` min_samples).

    Bias: 'bullish' | 'bearish' | 'neutral'. Levels: the clustered strikes.
    """
    out = _base_result("unusual_volume_burst")
    ctx = context if context is not None else new_context()
    s = _resolve_spot(flow_profile, spot)

    cold = _cold_start_note(flow_profile)
    unusual = list(getattr(flow_profile, "unusual_strikes", None) or [])
    if cold is not None or not unusual:
        out["details"]["cold_start"] = bool(getattr(flow_profile, "cold_start", False))
        out["rationale"] = (
            "No directional read: " + (cold if cold is not None else
            "no unusual strikes above threshold in this snapshot")
            + ". EXPERIMENTAL — baselines are cold-start until forward "
            "capture accrues contract history; see "
            "backtests/reports/flow-volume-burst.md."
        )
        return out

    top = sorted(unusual, key=lambda u: float(u.get("score", 0.0)),
                 reverse=True)[:max(1, int(top_n))]
    calls = [u for u in top if str(u.get("option_type", "")).lower() == "call"]
    puts = [u for u in top if str(u.get("option_type", "")).lower() == "put"]
    side, side_list = ("calls", calls) if len(calls) >= len(puts) else ("puts", puts)
    if not side_list:
        out["rationale"] = "Top unusual strikes are side-mixed — no directional read (experimental)."
        return out

    strikes = [float(u["strike"]) for u in side_list
               if _as_float(u.get("strike")) is not None]
    # Directional clustering: calls above spot = upside flow,
    # puts below spot = downside flow. Same-side strikes on the "wrong"
    # side of spot carry no directional meaning -> neutral.
    if side == "calls" and all(k > s for k in strikes):
        bias = "bullish"
    elif side == "puts" and all(k < s for k in strikes):
        bias = "bearish"
    else:
        bias = "neutral"

    out["bias"] = bias
    out["levels"] = sorted(strikes)
    out["details"] = {
        "spot": s,
        "dominant_side": side,
        "n_calls": len(calls),
        "n_puts": len(puts),
        "top_scores": [round(float(u.get("score", 0.0)), 2) for u in top],
    }
    out["rationale"] = (
        f"EXPERIMENTAL hypothesis: {len(side_list)}/{len(top)} top unusual "
        f"strikes are {side} at {sorted(strikes)} vs spot {s:.2f}; bias "
        f"{bias} = continuation in that direction. Baselines: NOT cold "
        "start for these strikes (they carried usable baselines). "
        "Unvalidated — see backtests/reports/flow-volume-burst.md. Do not "
        "trade on this."
    )
    if ctx.get("last_signal") != ("unusual_volume_burst", bias):
        ctx["last_signal"] = ("unusual_volume_burst", bias)
    return out


def sweep_followthrough(flow_profile: Any, spot: Optional[float] = None,
                        context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Sweep follow-through: proxy-flagged sweep events -> hypothesized directional pressure.

    EXPERIMENTAL — hypothesis only. The claim under test: a sweep-like
    event (see ``src/turblance_trader/flow/sweep.py``) leaves directional
    pressure over the next N sessions — lifted calls = bullish, hit calls =
    bearish, lifted puts = bearish, hit puts = bullish. Validation:
    ``backtests/reports/flow-sweep-followthrough.md`` — did price move in
    the hypothesized direction within N sessions after the event?

    RATIONALE CAVEAT: sweeps are UNCONFIRMED proxies until IBKR tick data.
    A chain snapshot shows aggressive prints + elevated volume across
    strikes; independent orders in one snapshot window can look identical.
    Every event carries ``proxy=True`` and ``experimental=True`` at the
    engine layer. Until tick-level trade data (time, price, size, exchange
    per print) confirms events, this signal scores a *proxy*, not a sweep.

    Bias: 'bullish' | 'bearish' | 'neutral'. Levels: strikes of the top event.
    """
    out = _base_result("sweep_followthrough")
    ctx = context if context is not None else new_context()
    _resolve_spot(flow_profile, spot)  # validates spot availability

    sweeps = list(getattr(flow_profile, "sweeps", None) or [])
    if not sweeps:
        out["rationale"] = (
            "No sweep-like events in this snapshot — no follow-through "
            "signal. EXPERIMENTAL; events are proxies until IBKR tick "
            "data confirms them. See backtests/reports/"
            "flow-sweep-followthrough.md."
        )
        return out

    # direction: 'calls' | 'puts'; side: 'lifted' | 'hit'.
    # lifted calls / hit puts  -> bullish pressure
    # hit calls / lifted puts  -> bearish pressure
    top = max(sweeps, key=lambda e: float(e.get("score", 0.0) or 0.0))
    direction = str(top.get("direction", "")).lower()
    side = str(top.get("side", "")).lower()
    if (direction, side) in (("calls", "lifted"), ("puts", "hit")):
        bias = "bullish"
    elif (direction, side) in (("calls", "hit"), ("puts", "lifted")):
        bias = "bearish"
    else:
        bias = "neutral"

    strikes = [float(x) for x in (top.get("strikes_hit") or [])
               if _as_float(x) is not None]
    out["bias"] = bias
    out["levels"] = sorted(strikes)
    out["details"] = {
        "event_expiry": top.get("expiry"),
        "event_direction": direction,
        "event_side": side,
        "event_volume": _as_float(top.get("volume")),
        "event_score": _as_float(top.get("score")),
        "n_events": len(sweeps),
        "used_burst_fallback": bool(top.get("used_burst_fallback", False)),
        "proxy": True,
    }
    out["rationale"] = (
        f"EXPERIMENTAL hypothesis: top sweep-like event {direction}/{side} "
        f"across {len(strikes)} strikes (score {out['details']['event_score']}) "
        f"-> hypothesized {bias} pressure over the coming sessions. "
        "UNCONFIRMED PROXY: no tick data backs this event; independent "
        "orders can look identical. Confirmation requires IBKR tick-level "
        "trade data. Unvalidated — see "
        "backtests/reports/flow-sweep-followthrough.md. Do not trade on this."
    )
    ctx.setdefault("seen_events", []).append(
        {"signal": "sweep_followthrough", "bias": bias,
         "score": out["details"]["event_score"]})
    return out


def vex_regime(vex_profile: Any, spot: Optional[float] = None,
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Vol-regime IV read from the VEX profile's vol_regime label.

    EXPERIMENTAL — hypothesis only. Maps the dealer vega regime to a
    forward IV read: positive -> IV contraction, negative -> IV expansion,
    mixed -> uncertain. Units of every exposure input: dollars per 1 vol
    point (1%) move of IV (see ``src/turblance_trader/vex/exposure.py``).

    RATIONALE CAVEAT: the regime cutoffs are arbitrary heuristics
    (total_net_vex / sum|net_vex| with +/-0.5 thresholds in the VEX engine)
    and the mapping to IV direction is a mechanical dealer-hedging story,
    not an empirical finding. Nothing about this forecast has been
    measured. Validation: ``backtests/reports/vex-regime.md`` — does
    forward realized IV (proxied by realized volatility) move as forecast?

    Bias: 'contraction' | 'expansion' | 'uncertain' | 'unknown'.
    Levels: [vega_flip, put_vega_wall, call_vega_wall] when known.
    """
    out = _base_result("vex_regime")
    _resolve_spot(vex_profile, spot)  # validates spot availability
    regime_raw = getattr(vex_profile, "vol_regime", None)
    regime = str(regime_raw).strip().lower() if regime_raw is not None else "unknown"
    flip = _as_float(getattr(vex_profile, "vega_flip", None))
    call_wall = _as_float(getattr(vex_profile, "call_vega_wall", None))
    put_wall = _as_float(getattr(vex_profile, "put_vega_wall", None))
    king = getattr(vex_profile, "vex_king", None) or {}
    out["levels"] = sorted(v for v in (put_wall, flip, call_wall) if v is not None)
    out["details"]["vol_regime"] = regime
    out["details"]["vega_flip"] = flip
    out["details"]["vex_king_strike"] = _as_float(
        king.get("strike") if isinstance(king, dict) else getattr(king, "strike", None))

    forecast = VOL_REGIME_FORECAST.get(regime)
    if forecast is None:
        out["bias"] = "unknown"
        out["rationale"] = (
            f"Vol regime '{regime_raw}' is not one of positive/negative/mixed "
            "— no IV read (experimental)."
        )
        return out
    bias, explanation = forecast
    out["bias"] = bias
    out["rationale"] = (
        f"EXPERIMENTAL: {explanation} The +/-0.5 regime cutoffs are "
        "arbitrary heuristics and the IV mapping is a mechanical "
        "dealer-hedging story, not a measured effect. Unvalidated — see "
        "backtests/reports/vex-regime.md. Do not trade on this."
    )
    return out


def darkpool_divergence(ats_df: Any, weekly_prices: Any = None,
                        symbol: Optional[str] = None,
                        context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Weekly ATS dark-volume share vs price-trend divergence (hypothesis only).

    EXPERIMENTAL — hypothesis only, and DELAYED DATA ONLY: FINRA ATS
    weekly data is ~2 weeks old by design for Tier 1 symbols, so this
    signal can never be a timing trigger; it is a structure/research read.

    The claim under test: when a symbol's ATS weekly share volume rises
    week-over-week while its price trends DOWN (dark accumulation) or ATS
    volume spikes while price trends UP (dark distribution), the
    divergence predicts trend continuation over the following weeks.
    ``ats_df`` follows the darkpool ATS contract (columns: week_start,
    symbol, ats_mpid, weekly_shares, ...); ``weekly_prices`` is an optional
    DataFrame/iterable of (week_start, close) aligned to the same weeks.

    Bias: 'accumulation' | 'distribution' | 'neutral'. Levels: [].
    """
    out = _base_result("darkpool_divergence")
    ctx = context if context is not None else new_context()

    if ats_df is None or len(ats_df) == 0:
        out["rationale"] = (
            "No ATS data supplied — no divergence read. EXPERIMENTAL; "
            "FINRA ATS data is ~2 weeks delayed by design and cannot drive "
            "timing decisions. Hypothesis only."
        )
        return out

    frame = ats_df.copy()
    if symbol is not None:
        frame = frame[frame["symbol"].astype(str).str.upper() == str(symbol).upper()]
    if len(frame) == 0:
        out["rationale"] = (
            f"No ATS rows for symbol '{symbol}' — no divergence read "
            "(experimental)."
        )
        return out

    weekly = (frame.groupby("week_start")["weekly_shares"].sum()
              .sort_index())
    out["details"]["n_weeks"] = int(len(weekly))
    out["details"]["latest_week"] = str(weekly.index[-1])
    if len(weekly) < 2:
        out["rationale"] = (
            "Fewer than 2 ATS weeks — week-over-week change is undefined; "
            "no divergence read (experimental)."
        )
        return out

    wow = (weekly.iloc[-1] - weekly.iloc[-2]) / weekly.iloc[-2] \
        if weekly.iloc[-2] else float("nan")
    out["details"]["wow_share_change"] = None if math.isnan(wow) else float(wow)

    price_trend = None
    if weekly_prices is not None and len(weekly_prices):
        closes = list(weekly_prices)
        # Accept DataFrame with (week_start, close) or plain (week, close) pairs.
        try:
            pairs = [(str(r[0]), float(r[1])) for r in closes]
        except Exception:
            pairs = []
        if len(pairs) >= 2:
            price_trend = "up" if pairs[-1][1] > pairs[-2][1] else \
                          "down" if pairs[-1][1] < pairs[-2][1] else "flat"
    out["details"]["price_trend"] = price_trend

    bias = "neutral"
    if not math.isnan(wow) and wow > 0 and price_trend == "down":
        bias = "accumulation"
    elif not math.isnan(wow) and wow > 0 and price_trend == "up":
        bias = "distribution"
    out["bias"] = bias
    out["rationale"] = (
        f"EXPERIMENTAL hypothesis only (delayed data: ATS prints are ~2 "
        f"weeks old for Tier 1). Latest ATS week {out['details']['latest_week']}: "
        f"week-over-week ATS share change "
        f"{out['details']['wow_share_change']}, price trend {price_trend} -> "
        f"read '{bias}'. A 'divergence' here is a structure observation, "
        "not a timing trigger, and it is UNVALIDATED. Do not trade on this."
    )
    ctx["last_signal"] = ("darkpool_divergence", bias)
    return out


def run_all(flow_profile: Any = None, vex_profile: Any = None,
            ats_df: Any = None, spot: Optional[float] = None,
            context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Run the flow/vex candidate signals that have a profile available.

    ``flow_profile`` feeds unusual_volume_burst + sweep_followthrough;
    ``vex_profile`` feeds vex_regime; ``ats_df`` feeds
    darkpool_divergence. Missing inputs simply omit that signal —
    nothing is fabricated.
    """
    outs: List[Dict[str, Any]] = []
    if flow_profile is not None:
        outs.append(unusual_volume_burst(flow_profile, spot, context))
        outs.append(sweep_followthrough(flow_profile, spot, context))
    if vex_profile is not None:
        outs.append(vex_regime(vex_profile, spot, context))
    if ats_df is not None:
        outs.append(darkpool_divergence(ats_df, context=context))
    return outs
