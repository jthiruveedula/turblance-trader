"""IBKR (Interactive Brokers) adapter — TWS socket API via ib_insync/ib_async.

Connection approach (decided 2026-09-20, see README "Going live with IBKR"):
ib_insync-style library + locally running IB Gateway, over the TWS socket
API. Rejected alternative: the Client Portal Web API — it still needs a
Client Portal Gateway process running next to the app AND its session
expires roughly daily, forcing interactive 2FA re-auth. That is hostile to
an unattended cron pipeline. The socket API has no API keys (auth happens
once at gateway login) and the session survives gateway auto-restarts.

Data mechanics (TWS API, verified against IBKR docs + community references):
  * Chain discovery:  reqSecDefOptParams(underlying) -> expirations/strikes
  * Per contract:     reqMktData(snapshot=True, genericTickList='100,101,106')
      - generic tick 100 -> option volume   (tickSize 29 call / 30 put)
      - generic tick 101 -> option open int (tickSize 27 call / 28 put)
      - generic tick 106 -> option IV       (tickGeneric 24)
      - tickOptionComputation (13) -> model Greeks incl. undPrice
  * Greeks require market-data entitlements for BOTH the option (OPRA) and
    the underlying (see README for the subscription list).

Defensive design:
  * ib_insync (archived upstream; ib_async is the maintained fork with the
    same interface) is an OPTIONAL dependency. If neither is importable,
    fetch raises DependencyMissingError with install instructions.
    Nothing is pip-installed by this code, ever.
  * Tests inject a fake connection factory, so adapter logic is fully
    covered without a live gateway.
  * Connection settings come from env vars (IBKR_HOST / IBKR_PORT /
    IBKR_CLIENT_ID / IBKR_MARKET_DATA_TYPE) or an optional INI file pointed
    to by IBKR_CONFIG. No credentials are hardcoded or committed — and note
    the socket API needs NO password in code at all; login/2FA happens in
    the gateway itself (see README for the 2FA story).
"""

from __future__ import annotations

import configparser
import os
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from turblance_trader.capture.adapters.base import (
    ChainAdapter,
    ChainSourceError,
    SetupRequiredError,
)
from turblance_trader.capture.flow import TICK_COLUMNS, validate_tick_schema

# Verified TWS protocol tick-type numbers (generic tick -> delivered tick).
_GENERIC_TICKS = "100,101,106"  # option volume, option OI, option IV
_TICK_OI = {27, 28}             # call / put open interest (tickSize)
_TICK_VOLUME = {29, 30}         # call / put volume (tickSize)
_TICK_IV = 24                   # option implied volatility (tickGeneric)

_UNDERLYING_EXCHANGE = {"SPX": ("Index", "CBOE")}


class DependencyMissingError(SetupRequiredError):
    """The optional ib_insync/ib_async dependency is not installed."""


def _load_ib():
    """Import ib_insync, falling back to its maintained fork ib_async."""
    try:
        import ib_insync  # type: ignore

        return ib_insync
    except ImportError:
        pass
    try:
        import ib_async  # type: ignore

        return ib_async
    except ImportError:
        pass
    raise DependencyMissingError(
        "IBKR adapter needs the 'ib_insync' package (or its maintained fork "
        "'ib_async') and neither is installed. Install into a virtualenv "
        "(never into system python):\n"
        "  python3 -m venv ~/workspace/.venv && "
        "~/workspace/.venv/bin/pip install ib_async\n"
        "Then run the capture with that venv's python."
    )


def _read_settings() -> dict[str, Any]:
    """Connection settings: env vars, optionally overridden by IBKR_CONFIG file."""
    settings: dict[str, Any] = {
        "host": os.environ.get("IBKR_HOST", "127.0.0.1"),
        "port": int(os.environ.get("IBKR_PORT", "4001")),
        "client_id": int(os.environ.get("IBKR_CLIENT_ID", "11")),
        # 1 = live, 3 = delayed, 4 = delayed-frozen (works without subscriptions)
        "market_data_type": int(os.environ.get("IBKR_MARKET_DATA_TYPE", "1")),
        "batch_size": int(os.environ.get("IBKR_BATCH_SIZE", "60")),
        "pacing_delay": float(os.environ.get("IBKR_PACING_DELAY", "1.0")),
    }
    config_path = os.environ.get("IBKR_CONFIG")
    if config_path:
        parser = configparser.ConfigParser()
        parser.read(config_path)
        if parser.has_section("ibkr"):
            for key, value in parser["ibkr"].items():
                if key in ("port", "client_id", "market_data_type", "batch_size"):
                    settings[key] = int(value)
                elif key == "pacing_delay":
                    settings[key] = float(value)
                elif key == "host":
                    settings[key] = value
    return settings


class IBKRAdapter(ChainAdapter):
    """Options-chain snapshots via a local IB Gateway + TWS socket API."""

    source_name = "ibkr"

    def __init__(
        self,
        ib_factory: Callable[[], Any] | None = None,
        ib_module: Any | None = None,
        **overrides: Any,
    ) -> None:
        """
        Args:
            ib_factory: zero-arg callable returning a connected-IB-like
                object. Injected by tests; when None, a real connection is
                built from ib_insync/ib_async (must be installed).
            ib_module: the ib_insync/ib_async module (or a test double).
            **overrides: any setting key from _read_settings(), e.g.
                host/port/client_id/market_data_type/batch_size/pacing_delay.
        """
        self._ib_factory = ib_factory
        self._ibmod = ib_module  # explicit test double; None -> _load_ib() on use
        self.settings = _read_settings()
        self.settings.update({k: v for k, v in overrides.items() if v is not None})

    # ------------------------------------------------------------ connection
    def _module(self):
        if self._ibmod is None:
            self._ibmod = _load_ib()
        return self._ibmod

    def _connect(self):
        s = self.settings
        if self._ib_factory is not None:
            ib = self._ib_factory()
        else:
            ib = self._module().IB()
            ib.connect(s["host"], s["port"], clientId=s["client_id"])
        if not ib.isConnected():
            raise ChainSourceError(
                f"IBKR: could not connect to {s['host']}:{s['port']} "
                f"(clientId={s['client_id']}). Is IB Gateway running with API "
                "connections enabled? See README 'Going live with IBKR'."
            )
        ib.reqMarketDataType(s["market_data_type"])
        return ib

    @staticmethod
    def _disconnect(ib) -> None:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass

    # ------------------------------------------------------------------ fetch
    def _underlying(self, symbol: str):
        mod = self._module()
        kind, exchange = _UNDERLYING_EXCHANGE.get(symbol.upper(), ("Stock", "SMART"))
        cls = getattr(mod, kind)
        return cls(symbol.upper(), exchange)

    def fetch_raw(self, symbol: str) -> dict:
        symbol = symbol.upper()
        ib = self._connect()
        try:
            return self._fetch_chain(ib, symbol)
        finally:
            self._disconnect(ib)

    def _discover_contracts(self, ib, symbol: str) -> list:
        """Chain discovery: reqSecDefOptParams -> one Option per expiry/strike."""
        mod = self._module()
        underlying = self._underlying(symbol)
        ib.qualifyContracts(underlying)
        chains = ib.reqSecDefOptParams(
            underlying.symbol, "", underlying.secType, underlying.conId
        )
        chain = next((c for c in chains if c.exchange == "SMART"), None)
        if chain is None:
            raise ChainSourceError(f"IBKR: no SMART option chain for {symbol}")
        contracts = [
            mod.Option(symbol, expiry, strike, right, "SMART")
            for expiry in sorted(chain.expirations)
            for strike in sorted(chain.strikes)
            for right in ("C", "P")
        ]
        ib.qualifyContracts(*contracts)
        return contracts

    def _fetch_chain(self, ib, symbol: str) -> dict:
        s = self.settings
        contracts = self._discover_contracts(ib, symbol)

        quote_time = datetime.now(timezone.utc).isoformat()
        spot = self._spot_from_first(ib, contracts, s)

        rows: list[dict] = []
        for batch in _chunks(contracts, s["batch_size"]):
            for contract in batch:
                row = self._snapshot_contract(ib, contract, s)
                if row is not None:
                    rows.append(row)
            ib.sleep(s["pacing_delay"])  # pacing: stay well under IBKR limits

        if not rows:
            raise ChainSourceError(f"IBKR: no contract snapshots returned for {symbol}")
        return {"symbol": symbol, "quote_time": quote_time, "spot": spot,
                "contracts": rows}

    def _spot_from_first(self, ib, contracts, s) -> float:
        """Underlying price from the first contract's model Greeks (undPrice)."""
        row = self._snapshot_contract(ib, contracts[0], s)
        if row and row.get("spot"):
            return float(row["spot"])
        raise ChainSourceError("IBKR: could not determine underlying price")

    def _snapshot_contract(self, ib, contract, s) -> dict | None:
        """One snapshot request; returns a row dict or None on failure."""
        collected: dict[str, float] = {}

        def on_tick_size(ticker, tick_type, size):
            if tick_type in _TICK_OI:
                collected["open_interest"] = float(size)
            elif tick_type in _TICK_VOLUME:
                collected["volume"] = float(size)

        def on_tick_generic(ticker, tick_type, value):
            if tick_type == _TICK_IV and value > 0:
                collected["iv"] = float(value)

        # ib_insync events support += ; the fake in tests mimics this.
        ib.tickSizeEvent += on_tick_size
        ib.tickGenericEvent += on_tick_generic
        try:
            ticker = ib.reqMktData(
                contract,
                genericTickList=_GENERIC_TICKS,
                snapshot=True,
                regulatorySnapshot=False,
                mktDataOptions=[],
            )
            ib.sleep(s["pacing_delay"])
        finally:
            try:
                ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001 - best effort
                pass
            ib.tickSizeEvent -= on_tick_size
            ib.tickGenericEvent -= on_tick_generic

        greeks = getattr(ticker, "modelGreeks", None)
        bid = _num(getattr(ticker, "bid", None))
        ask = _num(getattr(ticker, "ask", None))
        if bid is None and ask is None and greeks is None and not collected:
            return None  # no data arrived for this contract

        expiry = f"{contract.lastTradeDateOrContractMonth[0:4]}-" \
                 f"{contract.lastTradeDateOrContractMonth[4:6]}-" \
                 f"{contract.lastTradeDateOrContractMonth[6:8]}"
        return {
            "expiry": expiry,
            "strike": float(contract.strike),
            "option_type": "call" if contract.right == "C" else "put",
            "bid": bid,
            "ask": ask,
            "last": _num(getattr(ticker, "last", None)),
            "iv": collected.get("iv", _num(getattr(greeks, "impliedVol", None))),
            "open_interest": collected.get("open_interest", 0.0),
            "volume": collected.get("volume"),
            "delta": _num(getattr(greeks, "delta", None), allow_negative=True),
            "gamma": _num(getattr(greeks, "gamma", None), allow_negative=True),
            "theta": _num(getattr(greeks, "theta", None), allow_negative=True),
            "vega": _num(getattr(greeks, "vega", None), allow_negative=True),
            "spot": _num(getattr(greeks, "undPrice", None)),
        }

    # ------------------------------------------------------- tick prints
    #: tickType -> (ib_insync event attribute, supported)
    _TICK_EVENTS = {
        "Last": "tickByTickAllLastEvent",
        "AllLast": "tickByTickAllLastEvent",
    }

    def fetch_ticks(
        self,
        symbol: str,
        contracts: list | None = None,
        tick_type: str = "Last",
        max_ticks: int | None = None,
    ) -> pd.DataFrame | None:
        """Capture tick-by-tick trade prints via ``reqTickByTickData``.

        Returns a tick-print DataFrame (see :mod:`turblance_trader.capture.flow`)
        stored by callers under ``data/flow/ticks/`` — or ``None`` as a clean
        no-op when there is no gateway/dependency: ib_insync not installed, or
        the gateway unreachable. Also returns ``None`` when no prints arrived.

        Args:
            symbol: underlying, e.g. ``"SPY"``.
            contracts: pre-built option contracts; when None, the chain is
                discovered via ``reqSecDefOptParams`` (one extra call).
            tick_type: ``"Last"`` (exchange prints only) or ``"AllLast"``
                (all NBBO-captured prints).
            max_ticks: stop collecting per contract after this many prints
                (bounded runs for tests / sampling); None = run the pacing
                window.
        """
        if tick_type not in self._TICK_EVENTS:
            raise ValueError(
                f"tick_type must be one of {sorted(self._TICK_EVENTS)}; "
                f"got {tick_type!r}"
            )
        try:
            self._module()
        except DependencyMissingError:
            return None  # no ib_insync/ib_async installed: clean no-op
        try:
            ib = self._connect()
        except ChainSourceError:
            return None  # no gateway reachable: clean no-op
        try:
            return self._collect_ticks(ib, symbol, contracts, tick_type, max_ticks)
        finally:
            self._disconnect(ib)

    def _collect_ticks(self, ib, symbol, contracts, tick_type, max_ticks):
        symbol = symbol.upper()
        if contracts is None:
            contracts = self._discover_contracts(ib, symbol)
        event_name = self._TICK_EVENTS[tick_type]
        event = getattr(ib, event_name)
        s = self.settings
        capture_time = datetime.now(timezone.utc).isoformat()

        rows: list[dict] = []
        contract_of = {id(c): c for c in contracts}

        def on_print(ticker, tick):
            contract = contract_of.get(id(ticker.contract))
            if contract is None:
                return
            if max_ticks is not None and sum(
                1 for r in rows if r["_cid"] == id(contract)
            ) >= max_ticks:
                return
            expiry = (
                f"{contract.lastTradeDateOrContractMonth[0:4]}-"
                f"{contract.lastTradeDateOrContractMonth[4:6]}-"
                f"{contract.lastTradeDateOrContractMonth[6:8]}"
            )
            tick_time = getattr(tick, "time", None)
            if hasattr(tick_time, "isoformat"):
                tick_time = tick_time.isoformat()
            rows.append(
                {
                    "_cid": id(contract),
                    "symbol": symbol,
                    "capture_time": capture_time,
                    "expiry": expiry,
                    "strike": float(contract.strike),
                    "option_type": "call" if contract.right == "C" else "put",
                    "tick_time": str(tick_time),
                    "price": float(getattr(tick, "price", float("nan"))),
                    "size": getattr(tick, "size", None),
                    "tick_type": tick_type,
                }
            )

        event += on_print
        try:
            for contract in contracts:
                ticker = ib.reqTickByTickData(contract, tickType=tick_type)
                ib.sleep(s["pacing_delay"])
                try:
                    ib.cancelTickByTickData(ticker)
                except Exception:  # noqa: BLE001 - best effort
                    pass
        finally:
            event -= on_print

        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["_cid"] + TICK_COLUMNS).drop(
            columns=["_cid"]
        )
        df["size"] = pd.to_numeric(df["size"], errors="coerce").astype("Int64")
        return validate_tick_schema(df)

    # ------------------------------------------------------------------ parse
    def parse(self, raw: dict, symbol: str) -> pd.DataFrame:
        rows = []
        for c in raw["contracts"]:
            rows.append(
                {
                    "symbol": symbol.upper(),
                    "quote_time": raw["quote_time"],
                    "expiry": c["expiry"],
                    "strike": float(c["strike"]),
                    "option_type": c["option_type"],
                    "bid": _nan(c.get("bid")),
                    "ask": _nan(c.get("ask")),
                    "last": _nan(c.get("last")),
                    "implied_volatility": _nan(c.get("iv")),
                    "open_interest": int(c.get("open_interest") or 0),
                    "volume": c.get("volume"),
                    "delta": _nan_greek(c.get("delta")),
                    "gamma": _nan_greek(c.get("gamma")),
                    "theta": _nan_greek(c.get("theta")),
                    "vega": _nan_greek(c.get("vega")),
                    "spot": float(raw["spot"]),
                }
            )
        if not rows:
            raise ChainSourceError(f"IBKR: empty contract list for {symbol}")
        df = pd.DataFrame(rows)
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")
        df["open_interest"] = df["open_interest"].astype("Int64")
        return df


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i: i + size]


def _num(value, allow_negative: bool = False) -> float | None:
    """A finite float, else None.

    IBKR uses -1 / NaN for "no value". Prices, IV, volume, OI and spot are
    never negative, so negatives are rejected there; Greeks (delta, gamma,
    theta, vega) legitimately go negative, so they pass through.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if not allow_negative and f < 0:
        return None
    return f


def _nan(value) -> float:
    n = _num(value)
    return n if n is not None else float("nan")


def _nan_greek(value) -> float:
    """Like _nan but allows negative values (theta/vega go negative)."""
    n = _num(value, allow_negative=True)
    return n if n is not None else float("nan")
