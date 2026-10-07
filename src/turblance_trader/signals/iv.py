"""IV factors computed from a single chain snapshot — no history needed.

Clean-room original. These are mechanical measurements of the snapshot's
implied-volatility surface:

  * ATM IV — implied vol of the contract nearest spot on the nearest expiry.
  * Put-call skew — mean put IV minus mean call IV on the front expiry for
    strikes within +-5% of spot (how much downside protection is bid).
  * Term-structure slope — second-expiry ATM IV minus front-expiry ATM IV
    (positive = upward-sloping / "patience is cheap").

IV *rank* needs history and is honestly gated: it returns
``"insufficient_history"`` until 20+ trading days of ATM-IV observations
exist for the symbol. History is banked by the alert evaluator
(data/signals/atm_iv.json), one observation per symbol per day.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

MIN_HISTORY_DAYS = 20  # honest gate for iv_rank


def _finite(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").dropna()


def _front_expiry(df: pd.DataFrame) -> str | None:
    exps = sorted(df["expiry"].dropna().unique())
    return exps[0] if exps else None


def _atm_row(df: pd.DataFrame, spot: float, expiry: str) -> pd.Series | None:
    """Row nearest spot on ``expiry`` with a finite IV."""
    sub = df[(df["expiry"] == expiry)].copy()
    if sub.empty:
        return None
    sub["__d"] = (pd.to_numeric(sub["strike"], errors="coerce") - spot).abs()
    iv = pd.to_numeric(sub["implied_volatility"], errors="coerce")
    sub = sub[iv.notna()].sort_values("__d")
    if sub.empty:
        return None
    return sub.iloc[0]


def atm_iv(df: pd.DataFrame, spot: float | None = None) -> float | None:
    """ATM implied volatility: nearest-strike, nearest-expiry contract."""
    if df is None or df.empty:
        return None
    if spot is None or (isinstance(spot, float) and math.isnan(spot)):
        spot = pd.to_numeric(df["spot"], errors="coerce").median()
    if spot is None or math.isnan(float(spot)):
        return None
    expiry = _front_expiry(df)
    if expiry is None:
        return None
    row = _atm_row(df, float(spot), expiry)
    if row is None:
        return None
    return float(row["implied_volatility"])


def put_call_skew(
    df: pd.DataFrame,
    spot: float | None = None,
    band: float = 0.05,
    min_legs: int = 3,
) -> float | None:
    """Mean put IV minus mean call IV, front expiry, strikes within +-band.

    Positive = puts bid vs calls (downside fear priced). None when either
    side has fewer than ``min_legs`` usable contracts.
    """
    if df is None or df.empty:
        return None
    if spot is None or (isinstance(spot, float) and math.isnan(spot)):
        spot = pd.to_numeric(df["spot"], errors="coerce").median()
    if spot is None or math.isnan(float(spot)):
        return None
    expiry = _front_expiry(df)
    if expiry is None:
        return None
    sub = df[df["expiry"] == expiry].copy()
    strikes = pd.to_numeric(sub["strike"], errors="coerce")
    iv = pd.to_numeric(sub["implied_volatility"], errors="coerce")
    sub = sub[(strikes - spot).abs() / spot <= band]
    iv = iv.loc[sub.index]
    sub = sub[iv.notna()]
    puts = pd.to_numeric(sub.loc[sub["option_type"] == "put",
                                 "implied_volatility"], errors="coerce")
    calls = pd.to_numeric(sub.loc[sub["option_type"] == "call",
                                  "implied_volatility"], errors="coerce")
    if len(puts) < min_legs or len(calls) < min_legs:
        return None
    return float(puts.mean() - calls.mean())


def term_structure_slope(
    df: pd.DataFrame,
    spot: float | None = None,
) -> float | None:
    """Second-expiry ATM IV minus front-expiry ATM IV.

    Positive = upward-sloping term structure. None with < 2 expiries or
    missing ATM rows.
    """
    if df is None or df.empty:
        return None
    if spot is None or (isinstance(spot, float) and math.isnan(spot)):
        spot = pd.to_numeric(df["spot"], errors="coerce").median()
    if spot is None or math.isnan(float(spot)):
        return None
    exps = sorted(df["expiry"].dropna().unique())
    if len(exps) < 2:
        return None
    rows = [_atm_row(df, float(spot), e) for e in exps[:2]]
    if any(r is None for r in rows):
        return None
    return float(rows[1]["implied_volatility"] - rows[0]["implied_volatility"])


def iv_factors(df: pd.DataFrame, spot: float | None = None) -> dict:
    """All single-snapshot IV factors in one call.

    Plausibility gate: snapshots banked before the 2026-09-20 IV-unit fix
    carry IVs ~100x too small (e.g. ATM 0.001). An ATM IV below 2% is
    implausible for the index underlyings in the capture universe, so the
    factors come back None with an honest note instead of a wrong number.
    """
    out = {
        "atm_iv": atm_iv(df, spot),
        "put_call_skew": put_call_skew(df, spot),
        "term_structure_slope": term_structure_slope(df, spot),
        "experimental": True,
    }
    atm = out["atm_iv"]
    if atm is not None and atm < 0.02:
        out.update({
            "atm_iv": None,
            "put_call_skew": None,
            "term_structure_slope": None,
            "iv_note": ("snapshot IV implausibly low (<2% ATM) — likely "
                        "banked before the 2026-09-20 IV-unit fix; rescale "
                        "x100 or re-capture before trusting IV reads"),
        })
    return out


# ---------------------------------------------------------------------------
# ATM-IV history (for iv_rank). One observation per symbol per day.
# ---------------------------------------------------------------------------

def _load_history(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def record_atm_iv(
    symbol: str,
    atm: float | None,
    history_path: str | Path,
    day: str | None = None,
) -> bool:
    """Bank one ATM-IV observation. Returns True when a row was written.

    Refuses implausible values (<2% — the pre-2026-09-20-fix unit bug) so
    bad history can never accumulate silently.
    """
    if atm is None or (isinstance(atm, float) and math.isnan(atm)):
        return False
    if atm < 0.02:
        return False
    day = day or date.today().isoformat()
    hist = _load_history(history_path)
    rows = hist.setdefault(symbol, [])
    if any(r.get("date") == day for r in rows):
        return False  # one observation per day; no duplicates
    rows.append({"date": day, "atm_iv": float(atm)})
    rows.sort(key=lambda r: r["date"])
    Path(history_path).parent.mkdir(parents=True, exist_ok=True)
    Path(history_path).write_text(json.dumps(hist, indent=2))
    return True


def iv_rank(
    symbol: str,
    atm: float | None,
    history_path: str | Path,
) -> dict:
    """Percentile rank of today's ATM IV vs banked history.

    Honest cold start: returns {"status": "insufficient_history", "rank": None}
    until MIN_HISTORY_DAYS observations exist. Rank is the fraction of
    history at or below today's value, 0-100.
    """
    hist = _load_history(history_path).get(symbol, [])
    if atm is None or (isinstance(atm, float) and math.isnan(atm)):
        return {"status": "no_atm_iv", "rank": None, "n_days": len(hist)}
    if len(hist) < MIN_HISTORY_DAYS:
        return {
            "status": "insufficient_history",
            "rank": None,
            "n_days": len(hist),
            "needs_days": MIN_HISTORY_DAYS,
        }
    vals = [r["atm_iv"] for r in hist]
    rank = 100.0 * sum(1 for v in vals if v <= atm) / len(vals)
    return {"status": "ok", "rank": round(rank, 1), "n_days": len(hist)}
