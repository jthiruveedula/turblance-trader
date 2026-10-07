"""Dealer gamma-exposure (GEX) engine for turblance-trader.

Clean-room implementation: per-contract dollar gamma exposure from open
interest, aggregated into per-strike / per-expiry profiles with key levels
(zero-gamma flip, call/put walls, king node) and a gamma-regime read.

DEALER POSITIONING ASSUMPTION
-----------------------------
Market-maker positioning is not observed, so the engine applies an explicit,
flippable assumption: by default (``dealer_position='short'``) customers are
assumed to be the net buyers of options and dealers net SHORT those options,
so dealer GEX = -1 * customer GEX. Pass ``dealer_position='long'`` or
``'flat'`` to flip or zero the assumption. Everything in this package is
dealer-convention GEX.

EVIDENCE STATUS
---------------
Aggregations and level *measurements* are mechanical derivations of the
input chain. The *interpretations* attached to levels and regime
(support/resistance, range-day/trend-day heuristics) are EXPERIMENTAL and
UNVALIDATED until backtested — see backtests/README.md.
"""

from .levels import GEXProfile, compute_profile, gex_velocity

__all__ = ["GEXProfile", "compute_profile", "gex_velocity"]
