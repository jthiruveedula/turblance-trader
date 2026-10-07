"""Buy/sell suggestions from structure + flow + IV — with a confidence model.

Clean-room original. ``suggest()`` maps the instinct score, structural
levels, sweep flow, and IV factors to exactly one of six suggestions. The
mapping is a fixed, documented heuristic — EXPERIMENTAL and UNVALIDATED
until backtested. It is a personal research tool, NOT financial advice.

CONFIDENCE MODEL (enforced in code, documented in docs/signals-spec.md)
-----------------------------------------------------------------------
  * Structural levels (walls / flip / King node from today's chain) — HIGH.
    They are mechanical measurements of the snapshot.
  * Regime label (positive/negative/mixed gamma) — MEDIUM. It depends on
    the dealer-positioning assumption (flippable, unobserved).
  * Directional suggestion (the suggest() output) — LOW until >= 20 trading
    days of recorded signal history exist, then data-driven: the backtest
    harness scores suggestion outcomes and the label follows the measured
    hit rate. Until then, "LOW (N/20 days of signal history)".
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DISCLAIMER = "personal research tool, not financial advice"

SUGGESTIONS = (
    "CONSIDER_LONG_EXPOSURE",
    "CONSIDER_DOWNSIDE_PROTECTION",
    "FAVOR_DEFINED_RISK_PIN",
    "SCALE_EXITS_INTO_RESISTANCE",
    "ENTRIES_FAVORED_AT_SUPPORT",
    "NO_EDGE_QUIET",
)

MIN_SIGNAL_HISTORY_DAYS = 20


def sweep_direction(sweep: dict) -> int | None:
    """Map a sweep-proxy event to +1 (bullish) / -1 (bearish) / None.

    Aggressive call buying or put selling reads bullish; aggressive put
    buying or call selling reads bearish. Anything else (mid prints,
    unknown side) returns None — never guessed.
    """
    direction = str(sweep.get("direction", "")).lower()
    side = str(sweep.get("side", "")).lower()
    if direction == "calls" and side == "lifted":
        return 1
    if direction == "puts" and side == "hit":
        return 1
    if direction == "puts" and side == "lifted":
        return -1
    if direction == "calls" and side == "hit":
        return -1
    return None


def _fmt(x, digits=2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def suggest(
    instinct: dict,
    iv: dict,
    levels: dict,
    history_days: int = 0,
) -> dict:
    """One suggestion from instinct + IV + structural levels.

    ``instinct``: output of instinct_score().
    ``iv``: output of iv_factors() (keys atm_iv, put_call_skew,
    term_structure_slope).
    ``levels``: {"spot", "call_wall", "put_wall", "zero_gamma_flip",
    "king_node": {"strike": ...}} — None-safe.
    ``history_days``: trading days of recorded signal history (drives the
    directional confidence label).
    """
    score = int(instinct.get("score", 0))
    label = instinct.get("label", "NEUTRAL")
    pinned = bool(instinct.get("pinned", False))
    at_support = bool(instinct.get("at_support", False))
    at_resistance = bool(instinct.get("at_resistance", False))
    spot = levels.get("spot")
    cw, pw = levels.get("call_wall"), levels.get("put_wall")
    flip = levels.get("zero_gamma_flip")
    king = (levels.get("king_node") or {}).get("strike")
    skew = iv.get("put_call_skew")
    slope = iv.get("term_structure_slope")
    atm = iv.get("atm_iv")

    iv_note = ""
    if skew is not None and skew > 0.03:
        iv_note = f"; put skew elevated (+{_fmt(skew * 100, 1)}pts) — downside fear priced"
    elif skew is not None and skew < -0.01:
        iv_note = f"; call skew bid ({_fmt(skew * 100, 1)}pts) — upside chase priced"
    if slope is not None and slope > 0.02:
        iv_note += "; term structure upward-sloping — patience is cheap"

    drivers = [f"instinct {score} ({label})"]
    if at_support:
        drivers.append(f"at put-wall support {_fmt(pw, 0)}")
    if at_resistance:
        drivers.append(f"into call-wall resistance {_fmt(cw, 0)}")
    if pinned:
        drivers.append(f"pinned at King {_fmt(king, 0)}")
    if flip is not None and spot is not None:
        drivers.append(
            f"spot {'above' if spot > flip else 'below'} flip {_fmt(flip, 0)}")

    if abs(score) <= 14:
        suggestion = "NO_EDGE_QUIET"
        rationale = (f"score {score} ({label}): no edge — quiet{iv_note}")
    elif pinned:
        suggestion = "FAVOR_DEFINED_RISK_PIN"
        rationale = (f"pinned at King {_fmt(king, 0)}: chop expected; favor "
                     f"defined-risk pinning structures{iv_note}")
    elif at_resistance and score < 40:
        suggestion = "SCALE_EXITS_INTO_RESISTANCE"
        rationale = (f"into call-wall resistance {_fmt(cw, 0)}: scale exits "
                     f"into strength{iv_note}")
    elif at_support:
        suggestion = "ENTRIES_FAVORED_AT_SUPPORT"
        rationale = (f"at put-wall support {_fmt(pw, 0)}: entries favored at "
                     f"support{iv_note}")
    elif score >= 40:
        suggestion = "CONSIDER_LONG_EXPOSURE"
        rationale = (f"score {score} ({label}): structure constructive — "
                     f"consider long exposure{iv_note}")
    elif score <= -40:
        suggestion = "CONSIDER_DOWNSIDE_PROTECTION"
        rationale = (f"score {score} ({label}): structure fragile — consider "
                     f"downside protection{iv_note}")
    else:
        suggestion = "NO_EDGE_QUIET"
        rationale = (f"score {score} ({label}): lean only, no trigger — "
                     f"quiet{iv_note}")

    conf_level, conf_reason = directional_confidence(history_days)
    return {
        "suggestion": suggestion,
        "rationale": rationale,
        "drivers": drivers,
        "confidence": conf_level,
        "confidence_reason": conf_reason,
        "experimental": True,
        "disclaimer": DISCLAIMER,
    }


def directional_confidence(history_days: int) -> tuple[str, str]:
    """Confidence for a directional suggestion: LOW until 20 days of
    recorded signal history, then data-driven."""
    n = int(history_days or 0)
    if n < MIN_SIGNAL_HISTORY_DAYS:
        return ("LOW",
                f"only {n}/{MIN_SIGNAL_HISTORY_DAYS} trading days of signal "
                "history — unvalidated")
    return ("DATA-DRIVEN",
            f"{n} trading days of signal history — see backtest reports")


def count_signal_history_days(history_path: str | Path) -> int:
    """Distinct trading days with a recorded suggestion."""
    path = Path(history_path)
    if not path.exists():
        return 0
    try:
        rows = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    return len({r.get("date") for r in rows if r.get("date")})


def record_suggestion(
    symbol: str,
    suggestion: dict,
    score: int,
    history_path: str | Path,
    day: str | None = None,
) -> bool:
    """Bank one suggestion per symbol per day (drives the confidence gate)."""
    day = day or date.today().isoformat()
    path = Path(history_path)
    rows = []
    if path.exists():
        try:
            rows = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            rows = []
    if any(r.get("date") == day and r.get("symbol") == symbol for r in rows):
        return False
    rows.append({
        "date": day,
        "symbol": symbol,
        "suggestion": suggestion.get("suggestion"),
        "score": score,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2))
    return True


def confidence_report(levels: dict, history_days: int) -> dict:
    """Per-component confidence labels from the confidence model."""
    conf, reason = directional_confidence(history_days)
    return {
        "structural_levels": {
            "confidence": "HIGH",
            "reason": "mechanical measurement of today's chain",
        },
        "regime_label": {
            "confidence": "MEDIUM",
            "reason": "depends on the unobserved dealer-positioning assumption",
        },
        "directional_suggestion": {
            "confidence": conf,
            "reason": reason,
        },
    }
