#!/usr/bin/env python3
"""Post-market summary + tomorrow's steps for turblance-trader.

Reads the day's banked chain snapshots, builds a day recap per symbol
(spot range vs walls/flip/King, regime, sweeps, flow notes, IV read) and
tomorrow's plan (levels to watch, scenarios, bias + suggestion) with a
per-item confidence label from the signal confidence model.

Writes data/reports/postmarket_YYYY-MM-DD.md and prints the full summary
to stdout (the scheduler relays it to chat). Nothing here is financial
advice; every forward-looking line is experimental and unvalidated.

Exit codes:
  0  summary written (or cleanly skipped: no snapshots for the date)
  2  usage error
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.alerts.evaluate import evaluate_symbol  # noqa: E402
from turblance_trader.capture.market_hours import NYSE_HOLIDAYS  # noqa: E402
from turblance_trader.capture.store import read_snapshot  # noqa: E402
from turblance_trader.signals.suggestions import (  # noqa: E402
    confidence_report,
    count_signal_history_days,
)

CT = ZoneInfo("America/Chicago")
DEFAULT_SYMBOLS = ["SPY", "SPX", "QQQ", "IWM"]
DISCLAIMER = "Personal research tool, not financial advice. All forward-looking statements are experimental and unvalidated."


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", default=None,
                   help="YYYY-MM-DD in America/Chicago (default: today)")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    p.add_argument("--force", action="store_true",
                   help="run even for a weekend/holiday date (samples/backfill)")
    return p.parse_args(argv)


def day_snapshots(data_dir: Path, symbol: str, day: str) -> list[Path]:
    return sorted((data_dir / "chains" / symbol / day).glob("snapshot_*_utc.*"))


def _f(x, digits=1) -> str:
    return "n/a" if x is None else f"{x:,.{digits}f}"


def summarize_symbol(symbol: str, paths: list[Path], history_days: int) -> str:
    reads = []
    for p in paths:
        try:
            reads.append(evaluate_symbol(symbol, read_snapshot(p),
                                         history_days=history_days))
        except Exception as exc:  # one bad snapshot must not kill the day
            print(f"warning: skipping {p.name}: {exc}", file=sys.stderr)
    if not reads:
        return f"## {symbol}\n\nNo usable snapshots.\n"

    spots = [r["spot"] for r in reads if r["spot"] is not None]
    last = reads[-1]
    lv = last["levels"]
    inst = last["instinct"]
    sug = last["suggestion"]
    iv = last["iv"]
    conf = confidence_report(lv, history_days)

    # Sweeps seen across the day (union by key, keep max breadth).
    seen: dict[str, dict] = {}
    for r in reads:
        for sw in r["sweeps"]:
            k = sw["key"]
            if k not in seen or sw["n_strikes"] > seen[k]["n_strikes"]:
                seen[k] = sw
    sweeps = sorted(seen.values(), key=lambda s: -s["n_strikes"])

    lines = [f"## {symbol}",
             f"Snapshots: {len(reads)} · spot {_f(last['spot'])} "
             f"(day range {_f(min(spots))} – {_f(max(spots))})" if spots else "",
             "",
             "**Day recap**",
             f"- Gamma regime: {last['regime']} "
             f"(confidence {conf['regime_label']['confidence']})",
             f"- Vol regime: {last['vol_regime']}",
             f"- Instinct: {inst['score']} ({inst['label']})"
             f"{' — PINNED at King' if inst['pinned'] else ''}",
             f"- Levels (confidence {conf['structural_levels']['confidence']}): "
             f"call wall {_f(lv['call_wall'], 0)} · put wall {_f(lv['put_wall'], 0)} · "
             f"flip {_f(lv['zero_gamma_flip'], 0) if lv['zero_gamma_flip'] is not None else 'none (one-sided gamma)'} · "
             f"King {_f((lv['king_node'] or {}).get('strike'), 0)}",
             f"- Sweeps: {len(sweeps)} proxy event(s) today" +
             ("" if sweeps else " — flow quiet (cold start, baselines building)"),
             ]
    for sw in sweeps[:5]:
        lines.append(f"  - {sw['direction']} {sw['side']}, "
                     f"{sw['n_strikes']} strikes (proxy, experimental)")
    iv_note = iv.get("iv_note")
    if iv_note:
        lines.append(f"- IV: n/a — {iv_note}")
    else:
        atm = iv.get("atm_iv")
        skew = iv.get("put_call_skew")
        slope = iv.get("term_structure_slope")
        iv_line = f"- IV: ATM {_f(atm * 100 if atm else None)}%"
        if skew is not None:
            iv_line += f" · put-call skew {_f(skew * 100)}pts"
        if slope is not None:
            iv_line += f" · term slope {_f(slope * 100)}pts"
        lines.append(iv_line)
    lines += ["", "**Tomorrow's steps**"]
    cw, pw, flip = lv["call_wall"], lv["put_wall"], lv["zero_gamma_flip"]
    king = (lv["king_node"] or {}).get("strike")

    def step(text: str, c: str) -> str:
        return f"- [confidence {c}] {text}"

    lines.append(step(
        f"Watch {_f(cw, 0)} (call wall), {_f(pw, 0)} (put wall)"
        + (f", {_f(flip, 0)} (flip)" if flip is not None else ", no flip today")
        + f", {_f(king, 0)} (King). Levels are mechanical — HIGH confidence.",
        "HIGH"))
    if flip is not None:
        lines += [
            step(f"Above {_f(flip, 0)}: dealers long-gamma → moves dampened; "
                 f"upside room toward call wall {_f(cw, 0)}.", "MEDIUM"),
            step(f"Below {_f(flip, 0)}: dealers short-gamma → moves amplified; "
                 f"air pocket toward put wall {_f(pw, 0)}.", "MEDIUM"),
        ]
    if inst["pinned"]:
        lines.append(step(
            f"Pinned at King {_f(king, 0)} — expect chop; defined-risk "
            "pinning structures favored over directional bets.", "MEDIUM"))
    lines.append(step(
        f"Bias: {sug['suggestion']} — {sug['rationale']} "
        f"(directional confidence {sug['confidence']}: {sug['confidence_reason']})",
        sug["confidence"]))
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir)
    day = args.date or datetime.now(CT).date().isoformat()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    from datetime import date as _date
    d = _date.fromisoformat(day)
    is_trading_day = d.weekday() < 5 and d not in NYSE_HOLIDAYS
    if not is_trading_day and not args.force:
        print(f"{day} is not a trading day — skipping")
        return 0

    have = {s: day_snapshots(data_dir, s, day) for s in symbols}
    have = {s: p for s, p in have.items() if p}
    if not have:
        print(f"no snapshots banked for {day} — skipping")
        return 0

    history_days = count_signal_history_days(
        data_dir / "signals" / "signal_history.json")

    header = [
        f"# Post-market summary — {day}",
        "",
        f"Data: {', '.join(f'{s}×{len(p)}' for s, p in have.items())} "
        "snapshots (~15-min delayed CBOE).",
        "Status: EXPERIMENTAL — signals unvalidated; "
        f"{history_days}/20 trading days of signal history banked.",
        DISCLAIMER,
        "",
    ]
    body = "\n".join(header)
    for symbol, paths in have.items():
        body += summarize_symbol(symbol, paths, history_days) + "\n"

    out = data_dir / "reports" / f"postmarket_{day}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    print(body)
    print(f"\n[written to {out}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
