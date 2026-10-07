"""Alert evaluation: snapshots -> engines -> signals -> change alerts.

Clean-room original. ``evaluate_all()`` loads the latest banked snapshot per
symbol, runs the GEX/flow/VEX/signal engines, diffs against the persisted
state, and returns the alerts that fired. Pure logic lives here; the CLI
(script/evaluate_alerts.py) handles scheduling, gating, and output.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from turblance_trader.capture.store import read_snapshot
from turblance_trader.flow import compute_flow_profile
from turblance_trader.gex import compute_profile
from turblance_trader.signals import (
    instinct_score,
    iv_factors,
    suggest,
    sweep_direction,
)
from turblance_trader.signals.suggestions import (
    count_signal_history_days,
)
from turblance_trader.vex import compute_vex_profile

from .state import (
    already_fired,
    diff_alerts,
    load_state,
    mark_fired,
    new_day_state,
    save_state,
    sweep_key,
)


def latest_snapshots(
    data_dir: str | Path,
    symbols: list[str],
) -> dict[str, tuple[Path, pd.DataFrame]]:
    """Latest banked snapshot per symbol: {symbol: (path, df)}.

    A symbol with no snapshots is skipped silently.
    """
    out: dict[str, tuple[Path, pd.DataFrame]] = {}
    root = Path(data_dir) / "chains"
    for sym in symbols:
        paths = sorted((root / sym).rglob("snapshot_*_utc.*"))
        if not paths:
            continue
        out[sym] = (paths[-1], read_snapshot(paths[-1]))
    return out


def _sweep_inputs(sweeps: list[dict]) -> list[tuple[int, int]]:
    """Map flow sweep-proxy events to (direction, breadth) for instinct."""
    inputs = []
    for sw in sweeps:
        d = sweep_direction(sw)
        b = int(sw.get("n_strikes", 0) or 0)
        if d is not None and b > 0:
            inputs.append((d, b))
    return inputs


def evaluate_symbol(
    symbol: str,
    df: pd.DataFrame,
    history_days: int = 0,
) -> dict:
    """Full signal read for one symbol from one snapshot."""
    gex = compute_profile(df)
    vex = compute_vex_profile(df)
    flow = compute_flow_profile(df)
    gd = gex.to_dict()

    sweeps = []
    for sw in flow.sweeps:
        sweeps.append({
            "key": sweep_key(symbol, sw),
            "direction": sw.get("direction"),
            "side": sw.get("side"),
            "n_strikes": int(sw.get("n_strikes", 0) or 0),
            "score": sw.get("score"),
            "proxy": True,
            "experimental": True,
        })

    inst = instinct_score(
        S=gd["spot"],
        F=gd["zero_gamma_flip"],
        CW=gd["call_wall"],
        PW=gd["put_wall"],
        K=(gd["king_node"] or {}).get("strike"),
        R=gd["regime"],
        sweeps=_sweep_inputs(sweeps),
        V=vex.to_dict()["vol_regime"],
    )
    iv = iv_factors(df, spot=gd["spot"])
    levels = {
        "spot": gd["spot"],
        "call_wall": gd["call_wall"],
        "put_wall": gd["put_wall"],
        "zero_gamma_flip": gd["zero_gamma_flip"],
        "king_node": gd["king_node"],
    }
    sug = suggest(inst, iv, levels, history_days=history_days)

    return {
        "symbol": symbol,
        "spot": gd["spot"],
        "regime": gd["regime"],
        "vol_regime": vex.to_dict()["vol_regime"],
        "instinct": inst,
        "iv": iv,
        "suggestion": sug,
        "levels": levels,
        "sweeps": sweeps,
        "unusual_strikes": flow.unusual_strikes,
        "cold_start": bool(flow.cold_start),
    }


def evaluate_all(
    data_dir: str | Path,
    symbols: list[str],
    state_path: str | Path,
    today: str,
    history_path: str | Path | None = None,
) -> tuple[list[dict], dict]:
    """Evaluate every symbol and fire change alerts.

    Returns (alerts_fired, reads). Persists the updated state. Alerts that
    already fired today are never repeated.
    """
    state = load_state(state_path)
    if state.get("date") != today:
        state = new_day_state(today)

    history_days = (count_signal_history_days(history_path)
                    if history_path else 0)
    snapshots = latest_snapshots(data_dir, symbols)

    alerts: list[dict] = []
    reads: dict = {}
    for symbol, (path, df) in snapshots.items():
        read = evaluate_symbol(symbol, df, history_days=history_days)
        reads[symbol] = read

        prev = state["symbols"].get(symbol, {})
        curr = {
            "regime": read["regime"],
            "instinct_label": read["instinct"]["label"],
            "instinct_score": read["instinct"]["score"],
            "suggestion": read["suggestion"]["suggestion"],
            "spot": read["spot"],
            "levels": {
                "call_wall": read["levels"]["call_wall"],
                "put_wall": read["levels"]["put_wall"],
                "zero_gamma_flip": read["levels"]["zero_gamma_flip"],
            },
            "sweeps": read["sweeps"],
            "sweeps_seen": prev.get("sweeps_seen", []),
        }
        # First evaluation for a symbol establishes the baseline quietly:
        # there is no prior read to diff against, so nothing fires.
        if prev:
            for cand in diff_alerts(symbol, prev, curr, today):
                if not already_fired(state, cand["key"]):
                    mark_fired(state, cand["key"])
                    alerts.append(cand)

        # Persist today's read (prev for the next evaluation).
        curr["sweeps_seen"] = sorted(set(curr["sweeps_seen"]) |
                                     {s["key"] for s in read["sweeps"]})
        state["symbols"][symbol] = curr

    save_state(state, state_path)
    return alerts, reads
