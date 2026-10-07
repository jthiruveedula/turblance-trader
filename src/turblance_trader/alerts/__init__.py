"""Alert state + diffing for turblance-trader.

Clean-room original. Alerts fire ONLY on meaningful change vs the last
evaluation, and never repeat the same alert twice in one day. State lives in
data/alerts/state.json (git-ignored, local research bookkeeping).
"""

from .evaluate import evaluate_all, evaluate_symbol, latest_snapshots
from .state import (
    already_fired,
    default_state_path,
    diff_alerts,
    load_state,
    mark_fired,
    new_day_state,
    save_state,
    sweep_key,
)

__all__ = [
    "evaluate_all",
    "evaluate_symbol",
    "latest_snapshots",
    "already_fired",
    "default_state_path",
    "diff_alerts",
    "load_state",
    "mark_fired",
    "new_day_state",
    "save_state",
    "sweep_key",
]
