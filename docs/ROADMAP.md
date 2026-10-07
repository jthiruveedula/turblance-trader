# Turblance Trader — Roadmap

## Phase 0 — Recon (in progress)
Map the competitive landscape from public sources: feature catalogs, data-feed
landscape (costs, licensing, free tiers), and public tech notes. Output feeds the
functional spec. Clean-room: public facts only, nothing copied.

## Phase 1 — Functional spec + architecture (needs your approval)
Full spec of every module, the data pipeline design, and the backtest evidence
standard. Nothing gets built until you sign off.

## Phase 2 — Data pipeline
Free/delayed feeds first. No paid subscriptions without your explicit approval.

## Phase 3 — Engines
1. Dealer-exposure (GEX) engine
2. Options-flow scanner
3. Charting
4. Paper-trading sandbox
5. AI agent

(Build order to be confirmed — currently weighing GEX-first vs flow-first.)

## Phase 4 — Backtest harness + validation
Every feature gets a historical validation report: methodology, sample sizes,
in-sample vs out-of-sample, and honest limitations. Unvalidated signals are
labeled as such, never presented as reliable.

## Phase 5 — Hardening + your review
Two weeks of testing during live market sessions (starting 2026-09-21), then ship.

## Non-goals
- Copying any existing product's code, UI, branding, or copy
- Real-money order routing without your explicit, per-action approval
- Paid data feeds without your explicit approval
