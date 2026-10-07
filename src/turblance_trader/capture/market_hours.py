"""US equity market-hours gating for the capture cron.

The runner exits quietly (code 0, no snapshot) outside market hours unless
``--force`` is passed. Regular session: Mon–Fri, 09:30–16:00 America/New_York,
minus NYSE holidays. Early closes (day after Thanksgiving, Christmas Eve,
July 3rd when it falls mid-week) are NOT modeled — the runner will simply
capture a few extra snapshots on those afternoons, which is harmless.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# NYSE holidays. Extend each December for the following year.
NYSE_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 12, 25), # Christmas
        # 2027
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 4, 2),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24), # Christmas (observed)
    }
)


def is_market_open(
    now: datetime | None = None,
    *,
    holidays: frozenset[date] | set[date] = NYSE_HOLIDAYS,
) -> bool:
    """True when ``now`` falls inside the regular US equity session.

    ``now`` may be naive (assumed America/New_York) or tz-aware. Defaults to
    the current time in America/New_York.
    """
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    if now.date() in holidays:
        return False
    session_time = now.time()  # naive wall-clock time in ET
    return _MARKET_OPEN <= session_time < _MARKET_CLOSE
