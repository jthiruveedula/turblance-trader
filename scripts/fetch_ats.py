#!/usr/bin/env python3
"""Weekly FINRA ATS pull. Intended schedule: ONCE PER WEEK (e.g. Sunday).

Downloads the latest fully-published Tier 1 report week
(~2-week delayed per FINRA's spec), parses it into the ATS contract
schema, and stores it under data/darkpool/{YYYY-MM-DD}.csv.

Politeness: single request, never more than one pull per 7 days
(enforced by the adapter — CadenceError without --force).

Exit codes:
  0  success, or cleanly skipped (week already on disk)
  2  usage error
  3  FINRA blocked us (HTTP 429/403) — hard stop, investigate
  4  fetch/parse/schema failure or cadence refusal

PERSONAL RESEARCH USE ONLY: FINRA's terms restrict these downloads to
non-commercial personal/professional use. Data stays local and is never
republished, re-served, or shown to third parties.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.darkpool.base import (  # noqa: E402
    ATSSourceError,
    CadenceError,
    RateLimitedError,
)
from turblance_trader.darkpool.finra_ats import (  # noqa: E402
    FinraAtsAdapter,
    latest_report_week,
)
from turblance_trader.darkpool.storage import (  # noqa: E402
    list_weeks,
    weekly_path,
    write_weekly,
)

DATA_DIR = REPO_ROOT / "data"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--week",
        default=None,
        help="Report week Monday YYYY-MM-DD (default: latest published Tier 1 week)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the week is on disk / cadence guard trips",
    )
    args = ap.parse_args(argv)

    week = args.week or latest_report_week()
    adapter = FinraAtsAdapter()

    existing = weekly_path(week, DATA_DIR)
    if existing.exists() and not args.force:
        print(f"skip: {week} already on disk -> {existing}")
        print(f"local weeks: {list_weeks(DATA_DIR)}")
        return 0

    try:
        df = adapter.capture_week(week, DATA_DIR, force=args.force)
    except RateLimitedError as exc:
        print(f"HARD STOP (rate limited): {exc}", file=sys.stderr)
        return 3
    except CadenceError as exc:
        print(f"cadence refusal: {exc}", file=sys.stderr)
        return 4
    except ATSSourceError as exc:
        print(f"ATS fetch failed: {exc}", file=sys.stderr)
        return 4

    try:
        path, written = write_weekly(df, DATA_DIR, force=args.force)
    except Exception as exc:  # noqa: BLE001 - report and exit
        print(f"write failed: {exc}", file=sys.stderr)
        return 4

    action = "wrote" if written else "exists"
    print(
        f"{week}: {action} {len(df)} ATS rows "
        f"({df['symbol'].nunique()} symbols, {df['ats_mpid'].nunique()} venues) "
        f"-> {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
