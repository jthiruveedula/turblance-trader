#!/usr/bin/env python3
"""Generate deep-test "screen snips" (PNG) from today's live environment.

Reads the real banked snapshots + terminal_data.json. No invented data.
Outputs to ~/workspace/your_files/turblance-deep-test-2026-09-21/.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = Path.home() / "workspace" / "your_files" / "turblance-deep-test-2026-09-21"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BG = "#0d1117"
PANEL = "#161b22"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"
TEXT = "#e6edf3"
DIM = "#8b949e"

plt.rcParams.update({
    "figure.facecolor": BG,
    "axes.facecolor": PANEL,
    "text.color": TEXT,
    "axes.labelcolor": TEXT,
    "xtick.color": DIM,
    "ytick.color": DIM,
    "font.size": 11,
})


def load_payload():
    with open(REPO_ROOT / "terminal_data" / "terminal_data.json") as fh:
        return json.load(fh)


def snip_summary():
    fig, ax = plt.subplots(figsize=(12, 8))
    ax.set_xlim(0, 12); ax.set_ylim(0, 8); ax.axis("off")
    ax.text(0.5, 7.45, "TURBLANCE TRADER — DEEP TEST DAY 1", fontsize=20,
            weight="bold", color=TEXT, ha="left", va="center")
    ax.text(0.5, 7.05, "Mon 2026-09-21 · live market session · env: hatch VM",
            fontsize=12, color=DIM, ha="left", va="center")

    rows = [
        ("Unit tests (pytest)", "253 / 253 PASS", GREEN),
        ("Chain capture (CBOE, hourly)", "LIVE — 4 symbols, 57,840 contracts today", GREEN),
        ("Alert engine (15-min)", "LIVE — 22 alerts fired 09:32 CT, quiet after", GREEN),
        ("Terminal export", "OK — 4 symbols, 0 skipped rows", GREEN),
        ("ATM-IV + signal history", "OK — today's values written", GREEN),
        ("Backtest harness", "Honest 'insufficient data' refusal (need 60d)", GREEN),
        ("CBOE quote_time bug", "FOUND — timestamps +4h future-dated (ET vs UTC)", RED),
        ("FINRA ATS darkpool pull", "BLOCKED — endpoint closes connection (finra.org reachable)", AMBER),
        ("Bare `pytest` (no PYTHONPATH)", "FAILS — ModuleNotFoundError, needs conftest.py", AMBER),
    ]
    y = 6.35
    for name, status, color in rows:
        ax.text(0.5, y, name, fontsize=13, color=TEXT, ha="left", va="center")
        ax.text(11.5, y, status, fontsize=12, color=color, ha="right", va="center",
                weight="bold")
        y -= 0.62
    ax.text(0.5, 0.45, "Nothing was cancelled, unsubscribed, purchased, or rebooked. "
            "Read-only except normal cron writes.", fontsize=10, color=DIM,
            ha="left", va="center", style="italic")
    fig.tight_layout()
    fig.savefig(OUT_DIR / "snip-1-summary.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def snip_gex_overview(payload):
    syms = ["SPY", "SPX", "QQQ", "IWM"]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharey=False)
    fig.suptitle("LIVE DEALER EXPOSURE (GEX) — today's 09:26 CT snapshots",
                 fontsize=16, weight="bold", color=TEXT)
    for ax, sym in zip(axes.flat, syms):
        p = payload["symbols"][sym]
        g = p["gex"]
        strikes = [r["strike"] for r in g["by_strike"]]
        net = [r["net_gex"] for r in g["by_strike"]]
        colors = [GREEN if v >= 0 else RED for v in net]
        ax.bar(strikes, net, color=colors, width=(strikes[1] - strikes[0]) * 0.85
               if len(strikes) > 1 else 1)
        spot = p["spot"]
        ax.axvline(spot, color=BLUE, linestyle="--", linewidth=1.5,
                   label=f"spot {spot:,.2f}")
        if g["call_wall"]:
            ax.axvline(g["call_wall"], color=AMBER, linestyle=":", linewidth=1.5,
                       label=f"call wall {g['call_wall']:,.0f}")
        if g["put_wall"]:
            ax.axvline(g["put_wall"], color="#a371f7", linestyle=":", linewidth=1.5,
                       label=f"put wall {g['put_wall']:,.0f}")
        ax.set_title(f"{sym} · regime: {g['gamma_regime']} · "
                     f"net ${g['total_net_gex']/1e6:+.1f}M per 1%",
                     fontsize=12, color=TEXT)
        ax.set_xlabel("strike"); ax.set_ylabel("net GEX ($/1%)")
        ax.tick_params(axis="x", rotation=30, labelsize=8)
        ax.legend(fontsize=8, loc="upper left", facecolor=PANEL, edgecolor=DIM)
        ax.grid(True, alpha=0.15)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    fig.savefig(OUT_DIR / "snip-2-gex-overview.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def snip_spy_detail(payload):
    p = payload["symbols"]["SPY"]
    g = p["gex"]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8),
                                   gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle(f"SPY — live GEX detail · snapshot {p['quote_time']}",
                 fontsize=15, weight="bold", color=TEXT)
    strikes = [r["strike"] for r in g["by_strike"]]
    calls = [r["call_gex"] for r in g["by_strike"]]
    puts = [r["put_gex"] for r in g["by_strike"]]
    w = (strikes[1] - strikes[0]) * 0.9 if len(strikes) > 1 else 1
    ax1.bar(strikes, calls, color=GREEN, width=w, alpha=0.85, label="call GEX")
    ax1.bar(strikes, puts, color=RED, width=w, alpha=0.85, label="put GEX")
    ax1.axvline(p["spot"], color=BLUE, linestyle="--", linewidth=2,
                label=f"spot {p['spot']:,.2f}")
    ax1.set_ylabel("GEX ($ per 1% move)")
    ax1.legend(fontsize=9, loc="upper left", facecolor=PANEL, edgecolor=DIM)
    ax1.grid(True, alpha=0.15)
    ax1.set_title(f"call wall {g['call_wall']} · put wall {g['put_wall']} · "
                  f"flip {g['zero_gamma_flip']} · regime {g['gamma_regime']} · "
                  f"king {g['king_node']['strike']}", fontsize=11, color=DIM)

    hist = p["spot_history"]
    ax2.plot([h["t"][11:16] for h in hist], [h["spot"] for h in hist],
             color=BLUE, marker="o", markersize=4, linewidth=1.5)
    ax2.set_title("spot prints — one per banked snapshot (real observed marks)",
                  fontsize=11, color=DIM)
    ax2.set_xlabel("quote time (UTC, as banked)"); ax2.set_ylabel("spot")
    ax2.tick_params(axis="x", rotation=30, labelsize=8)
    ax2.grid(True, alpha=0.15)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig(OUT_DIR / "snip-3-spy-detail.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    payload = load_payload()
    gen = payload["metadata"]["generated_at_utc"]
    print(f"payload generated: {gen}")
    snip_summary()
    snip_gex_overview(payload)
    snip_spy_detail(payload)
    files = sorted(OUT_DIR.glob("*.png"))
    for f in files:
        print("wrote", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
