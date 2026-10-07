"""Adapter registry."""

from turblance_trader.capture.adapters.alpaca import AlpacaAdapter
from turblance_trader.capture.adapters.base import (
    ChainAdapter,
    ChainSourceError,
    RateLimitedError,
    SetupRequiredError,
)
from turblance_trader.capture.adapters.cboe import CboeDelayedAdapter
from turblance_trader.capture.adapters.ibkr import IBKRAdapter
from turblance_trader.capture.adapters.schwab import SchwabAdapter

ADAPTERS: dict[str, type[ChainAdapter]] = {
    "cboe": CboeDelayedAdapter,
    "ibkr": IBKRAdapter,
    "schwab": SchwabAdapter,
    "alpaca": AlpacaAdapter,
}

__all__ = [
    "ADAPTERS",
    "ChainAdapter",
    "ChainSourceError",
    "RateLimitedError",
    "SetupRequiredError",
    "CboeDelayedAdapter",
    "IBKRAdapter",
    "SchwabAdapter",
    "AlpacaAdapter",
]
