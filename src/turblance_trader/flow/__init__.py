"""Unusual-activity + sweep-proxy flow engine for turblance-trader.

Clean-room implementation: per-contract volume/OI velocity vs rolling
baselines, plus sweep-LIKE detection from chain snapshots without tick
data. Public methodology only; no proprietary code, data, assets, or
branding are used or copied.

EVIDENCE STATUS
---------------
Aggregations and z-score *measurements* are mechanical derivations of the
input chain. Every *interpretation* (a high score meaning informed flow,
a cluster meaning a real sweep) is EXPERIMENTAL and UNVALIDATED until a
backtest report says otherwise. The sweep detector in particular finds
PROXIES — aggressive prints plus elevated volume across several strikes —
which independent orders can mimic. Real sweep confirmation needs
tick-level trade data (planned via the IBKR path later). Do not trade on
these outputs. This engine performs detection only; there is deliberately
no alert-delivery mechanism and no order routing.
"""

from .profile import FlowProfile, compute_flow_profile
from .sweep import classify_trade_location, detect_sweeps
from .unusual_activity import (
    BaselineSet,
    compute_baselines,
    score_unusual_activity,
)

__all__ = [
    "FlowProfile",
    "compute_flow_profile",
    "BaselineSet",
    "compute_baselines",
    "score_unusual_activity",
    "classify_trade_location",
    "detect_sweeps",
]
