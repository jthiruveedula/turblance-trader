# Turblance Trader

An original, research-grade trading terminal — built clean-room, aimed far beyond anything on the market.

**Status:** scaffolding · market recon in progress · build begins after spec approval

## What we're building

- **Charts + dealer positioning** — price charts layered with dealer exposure (GEX), gamma walls, and projections
- **Flow intelligence** — unusual options activity, dark-pool prints, institutional flow tracking
- **Paper-trading sandbox** — simulate strategies against live data; the terminal never routes real orders on its own
- **AI trading agent** — reads charts, positioning, and flow; explains its reasoning in plain language
- **Backtest harness** — every signal is validated on historical data with proper statistics before it ships

## Principles

- **Clean-room implementation.** We study what exists from public sources only. No third-party code, assets, branding, or copy — everything here is original.
- **Evidence before shipping.** No indicator, signal, or strategy goes live without backtested, statistically sound validation.
- **No real money moves without explicit approval.** This is a research and paper-trading tool unless you say otherwise.

## Testing plan

- Deep testing begins **Mon 2026-09-21**, during live market sessions, running **two weeks**
- Ship after the testing window, pending your review

## Layout

```
src/turblance_trader/   core package (engines: charts, exposure, flow, paper, agent)
backtests/              backtest harness + per-feature validation reports
data/                   local caches only — no credentials, no API keys in this repo
docs/                   specs, architecture, research notes
```

## Getting started

Build system and dev setup land with the first engine. Until then, see `docs/ROADMAP.md`.
