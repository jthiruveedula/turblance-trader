"""Persistent alert state (data/alerts/state.json) and change diffing.

Alerts fire ONLY on meaningful change vs the last evaluation, and never
repeat the same alert twice in one day. Dedupe resets each trading day.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_FILENAME = "state.json"


def default_state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "alerts" / STATE_FILENAME


def load_state(state_path: str | Path) -> dict:
    path = Path(state_path)
    if not path.exists():
        return {"date": None, "symbols": {}, "fired": []}
    try:
        st = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"date": None, "symbols": {}, "fired": []}
    st.setdefault("symbols", {})
    st.setdefault("fired", [])
    return st


def save_state(state: dict, state_path: str | Path) -> None:
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def new_day_state(today: str) -> dict:
    """Fresh state for a new trading day (dedupe resets daily)."""
    return {"date": today, "symbols": {}, "fired": []}


def already_fired(state: dict, key: str) -> bool:
    return key in state.get("fired", [])


def mark_fired(state: dict, key: str) -> None:
    if key not in state["fired"]:
        state["fired"].append(key)


def sweep_key(symbol: str, sweep: dict) -> str:
    strikes = ",".join(str(s) for s in sweep.get("strikes_hit", []))
    return (f"{symbol}:{sweep.get('expiry')}:{sweep.get('direction')}:"
            f"{sweep.get('side')}:{strikes}")


def _crossed(prev: float | None, new: float | None, level: float | None) -> str | None:
    """'above' / 'below' when spot crossed ``level`` between evaluations."""
    if prev is None or new is None or level is None:
        return None
    d0, d1 = prev - level, new - level
    if d0 == 0 or d1 == 0 or (d0 > 0) == (d1 > 0):
        return None
    return "above" if d1 > 0 else "below"


def diff_alerts(
    symbol: str,
    prev: dict,
    curr: dict,
    today: str,
) -> list[dict]:
    """Compare one symbol's previous vs current read; return alert dicts.

    ``prev``/``curr`` each carry: regime, instinct_label, instinct_score,
    suggestion, spot, levels {"call_wall","put_wall","zero_gamma_flip"},
    sweeps [{"key","direction","side","n_strikes"}], sweeps_seen [keys].
    Each alert: {"key", "kind", "text"}. The caller dedupes via fired keys.
    """
    alerts: list[dict] = []

    def add(kind: str, detail: str, text: str):
        alerts.append({"key": f"{today}:{symbol}:{kind}:{detail}",
                       "kind": kind, "text": text})

    # 1. Regime flip.
    if prev.get("regime") is not None and curr.get("regime") is not None:
        if prev["regime"] != curr["regime"]:
            add("regime", f"{prev['regime']}->{curr['regime']}",
                f"REGIME FLIP: {symbol} gamma regime "
                f"{prev['regime']} -> {curr['regime']}")

    # 2. Instinct crossing the +-40 bands.
    def banded(label: str) -> str:
        return ("bullish" if label == "BULLISH"
                else "bearish" if label == "BEARISH" else "mid")

    pb = banded(prev.get("instinct_label", ""))
    cb = banded(curr.get("instinct_label", ""))
    if pb != cb and (pb != "mid" or cb != "mid"):
        add("instinct",
            f"{prev.get('instinct_label')}->{curr.get('instinct_label')}",
            f"INSTINCT: {symbol} {prev.get('instinct_label')} -> "
            f"{curr.get('instinct_label')} (score {curr.get('instinct_score')})")

    # 3. New sweep with breadth >= 3.
    prev_sweeps = set(prev.get("sweeps_seen", []))
    for sw in curr.get("sweeps", []):
        if sw.get("n_strikes", 0) >= 3 and sw["key"] not in prev_sweeps:
            add("sweep", sw["key"],
                f"SWEEP: {symbol} {sw.get('direction')} {sw.get('side')} "
                f"{sw.get('n_strikes')} strikes (proxy, experimental)")

    # 4. Suggestion change.
    if prev.get("suggestion") is not None and curr.get("suggestion") is not None:
        if prev["suggestion"] != curr["suggestion"]:
            add("suggestion", f"{prev['suggestion']}->{curr['suggestion']}",
                f"SUGGESTION: {symbol} {prev['suggestion']} -> "
                f"{curr['suggestion']}")

    # 5. Spot crossing a wall / flip.
    levels = curr.get("levels", {})
    for name, label in (("call_wall", "call wall"),
                        ("put_wall", "put wall"),
                        ("zero_gamma_flip", "zero-gamma flip")):
        crossed = _crossed(prev.get("spot"), curr.get("spot"), levels.get(name))
        if crossed:
            lvl = levels[name]
            add("cross", f"{name}:{crossed}",
                f"LEVEL CROSS: {symbol} spot crossed {crossed} {label} "
                f"{lvl:g}")

    return alerts
