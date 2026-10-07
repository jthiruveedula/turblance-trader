"""Dealer vega-exposure (VEX) engine for turblance-trader.

Clean-room implementation: per-contract dollar vega exposure from open
interest, aggregated into per-strike / per-expiry profiles with key levels
(vega flip, call/put vega walls, VEX king node) and a vol-regime read.

UNIT CONTRACT
-------------
All VEX figures are in **dollars per 1 vol point (1%) move of implied
volatility**. The source ``vega`` column (and the Black-Scholes fallback)
is assumed to be quoted per 1.0 (100%) IV move, so it is divided by
``VOL_POINT_DIVISOR = 100.0``; see ``exposure.source_vega_iv_unit`` to
declare a different feed convention.

DEALER POSITIONING ASSUMPTION
-----------------------------
Market-maker positioning is not observed, so the engine applies an explicit,
flippable assumption: by default (``dealer_position='short'``) customers are
assumed to be the net buyers of options and dealers net SHORT those options,
so dealer VEX = -1 * customer VEX. Pass ``dealer_position='long'`` or
``'flat'`` to flip or zero the assumption. Everything in this package is
dealer-convention VEX.

EVIDENCE STATUS
---------------
Aggregations and level *measurements* are mechanical derivations of the
input chain. The *interpretations* attached to levels and regime
(vol-supply zones, vol-regime reads) are EXPERIMENTAL and UNVALIDATED until
backtested — see backtests/README.md.
"""

from .levels import VEXProfile, compute_vex_profile, vex_velocity

__all__ = ["VEXProfile", "compute_vex_profile", "vex_velocity"]
