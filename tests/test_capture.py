"""Unit tests for the forward options-chain capture pipeline.

No network access: the CBOE adapter is tested against a small saved fixture
JSON, and market-hours gating is tested with injected datetimes.
"""

import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.capture.adapters import (  # noqa: E402
    ADAPTERS,
    AlpacaAdapter,
    CboeDelayedAdapter,
    IBKRAdapter,
    SchwabAdapter,
    SetupRequiredError,
)
from turblance_trader.capture.adapters import ibkr as ibkr_mod  # noqa: E402
from turblance_trader.capture.adapters.base import ChainSourceError  # noqa: E402
from turblance_trader.capture.adapters.ibkr import DependencyMissingError  # noqa: E402
from turblance_trader.capture.market_hours import ET, is_market_open  # noqa: E402
from turblance_trader.capture.schema import (  # noqa: E402
    SCHEMA_COLUMNS,
    SchemaError,
    validate_schema,
)
from turblance_trader.capture.store import (  # noqa: E402
    read_snapshot,
    snapshot_path,
    write_snapshot,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "cboe_spy_sample.json"


def load_fixture_df() -> pd.DataFrame:
    raw = json.loads(FIXTURE.read_text())
    return CboeDelayedAdapter().parse(raw, "SPY")


class TestCboeParsing(unittest.TestCase):
    def setUp(self):
        self.df = load_fixture_df()

    def test_schema_columns_exact(self):
        self.assertEqual(list(self.df.columns), SCHEMA_COLUMNS)
        validate_schema(self.df)  # must not raise

    def test_contract_id_parsing(self):
        # SPY260918C00660000 -> 2026-09-18 call @ 660.0
        sep_call = self.df[
            (self.df["expiry"] == "2026-09-18") & (self.df["option_type"] == "call")
        ].iloc[0]
        self.assertEqual(sep_call["strike"], 660.0)
        # SPY260918P00660000 -> 2026-09-18 put @ 660.0
        sep_put = self.df[
            (self.df["expiry"] == "2026-09-18") & (self.df["option_type"] == "put")
        ].iloc[0]
        self.assertEqual(sep_put["strike"], 660.0)
        # SPY261016C00670000 -> 2026-10-16 call @ 670.0
        monthly = self.df[self.df["expiry"] == "2026-10-16"].iloc[0]
        self.assertEqual(monthly["strike"], 670.0)
        self.assertEqual(monthly["option_type"], "call")

    def test_quote_time_is_utc_iso8601(self):
        # Fixture timestamp "2026-09-18 15:59:00" is US Eastern -> 19:59 UTC.
        qt = self.df["quote_time"].iloc[0]
        self.assertEqual(qt, "2026-09-18T19:59:00+00:00")
        self.assertTrue((self.df["quote_time"] == qt).all())

    def test_iv_passed_through_as_decimal(self):
        # Real-world regression (2026-09-20): the CBOE endpoint quotes
        # per-contract "iv" in DECIMAL — ATM SPY contracts carried 0.1012
        # while the same payload's iv30 read 11.675 (percent). Fixture iv
        # 0.185 must reach the schema unchanged (0.185 = 18.5%).
        call_iv = self.df[self.df["option_type"] == "call"]["implied_volatility"].iloc[0]
        self.assertAlmostEqual(call_iv, 0.185)

    def test_spot_applied_to_all_rows(self):
        self.assertTrue((self.df["spot"] == 658.42).all())

    def test_missing_values_become_nan_or_na(self):
        monthly = self.df[self.df["expiry"] == "2026-10-16"].iloc[0]
        self.assertTrue(math.isnan(monthly["last"]))   # null last_trade_price
        self.assertTrue(pd.isna(monthly["volume"]))   # null volume

    def test_greeks_and_quotes_carried(self):
        call = self.df[self.df["option_type"] == "call"].iloc[0]
        self.assertAlmostEqual(call["bid"], 2.10)
        self.assertAlmostEqual(call["ask"], 2.25)
        self.assertAlmostEqual(call["delta"], 0.42)
        self.assertAlmostEqual(call["gamma"], 0.031)
        self.assertAlmostEqual(call["theta"], -0.15)
        self.assertAlmostEqual(call["vega"], 0.09)
        self.assertEqual(call["open_interest"], 1234)

    def test_unparseable_contract_skipped_not_fatal(self):
        raw = json.loads(FIXTURE.read_text())
        raw["data"]["options"].append({"option": "GARBAGE", "bid": 1.0})
        df = CboeDelayedAdapter().parse(raw, "SPY")
        self.assertEqual(len(df), 3)

    def test_empty_options_raises(self):
        raw = {"timestamp": "2026-09-18 15:59:00",
               "data": {"options": [], "current_price": 1.0}}
        with self.assertRaises(Exception):
            CboeDelayedAdapter().parse(raw, "SPY")

    def test_cboe_symbol_mapping(self):
        self.assertEqual(CboeDelayedAdapter.cboe_symbol("SPX"), "_SPX")
        self.assertEqual(CboeDelayedAdapter.cboe_symbol("SPY"), "SPY")
        self.assertEqual(CboeDelayedAdapter.cboe_symbol("QQQ"), "QQQ")
        self.assertEqual(CboeDelayedAdapter.cboe_symbol("IWM"), "IWM")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.df = load_fixture_df()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_path_layout(self):
        path = snapshot_path("SPY", "2026-09-18T19:59:00+00:00", self.tmp.name)
        rel = path.relative_to(self.tmp.name)
        self.assertEqual(
            str(rel),
            "chains/SPY/2026-09-18/snapshot_195900_utc.csv",
        )

    def test_write_read_round_trip(self):
        path, written = write_snapshot(self.df, self.tmp.name)
        self.assertTrue(written)
        self.assertTrue(path.exists())
        back = read_snapshot(path)
        self.assertEqual(list(back.columns), SCHEMA_COLUMNS)
        self.assertEqual(len(back), len(self.df))
        for col in ["symbol", "quote_time", "expiry", "option_type",
                    "strike", "bid", "ask", "spot", "open_interest"]:
            self.assertEqual(
                list(back[col].astype(str)), list(self.df[col].astype(str)), col
            )
        self.assertAlmostEqual(
            float(back["implied_volatility"].iloc[0]),
            float(self.df["implied_volatility"].iloc[0]),
        )

    def test_idempotent_without_force(self):
        path1, written1 = write_snapshot(self.df, self.tmp.name)
        path2, written2 = write_snapshot(self.df, self.tmp.name)
        self.assertTrue(written1)
        self.assertFalse(written2)
        self.assertEqual(path1, path2)

    def test_force_overwrites(self):
        path1, _ = write_snapshot(self.df, self.tmp.name)
        path2, written2 = write_snapshot(self.df, self.tmp.name, force=True)
        self.assertTrue(written2)
        self.assertEqual(path1, path2)


class TestMarketHours(unittest.TestCase):
    def test_regular_session_open(self):
        # Monday 2026-09-21 10:00 ET
        self.assertTrue(is_market_open(datetime(2026, 9, 21, 10, 0, tzinfo=ET)))

    def test_before_open_closed(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 21, 8, 0, tzinfo=ET)))

    def test_after_close_closed(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 21, 16, 30, tzinfo=ET)))

    def test_weekend_closed(self):
        self.assertFalse(is_market_open(datetime(2026, 9, 20, 12, 0, tzinfo=ET)))  # Sun
        self.assertFalse(is_market_open(datetime(2026, 9, 19, 12, 0, tzinfo=ET)))  # Sat

    def test_holiday_closed(self):
        # Thanksgiving 2026-11-26 and Christmas 2026-12-25
        self.assertFalse(is_market_open(datetime(2026, 11, 26, 11, 0, tzinfo=ET)))
        self.assertFalse(is_market_open(datetime(2026, 12, 25, 11, 0, tzinfo=ET)))

    def test_naive_datetime_assumed_et(self):
        self.assertTrue(is_market_open(datetime(2026, 9, 21, 10, 0)))
        self.assertFalse(is_market_open(datetime(2026, 9, 21, 8, 0)))


class TestSchemaValidation(unittest.TestCase):
    def test_rejects_wrong_option_type(self):
        df = load_fixture_df()
        df.loc[df.index[0], "option_type"] = "straddle"
        with self.assertRaises(SchemaError):
            validate_schema(df)

    def test_rejects_missing_column(self):
        df = load_fixture_df().drop(columns=["vega"])
        with self.assertRaises(SchemaError):
            validate_schema(df)

    def test_rejects_empty(self):
        with self.assertRaises(SchemaError):
            validate_schema(load_fixture_df().iloc[0:0])


class TestAdapterRegistry(unittest.TestCase):
    def test_registry_contents(self):
        self.assertEqual(
            ADAPTERS, {"cboe": CboeDelayedAdapter, "ibkr": IBKRAdapter,
                       "schwab": SchwabAdapter, "alpaca": AlpacaAdapter}
        )

    def test_stubs_raise_setup_error(self):
        for cls in (SchwabAdapter, AlpacaAdapter):
            with self.assertRaises(SetupRequiredError):
                cls().fetch_raw("SPY")
            with self.assertRaises(SetupRequiredError):
                cls().parse({}, "SPY")


# ---------------------------------------------------------------------------
# IBKR adapter — fake-connection tests (no live gateway, no ib_insync needed)
# ---------------------------------------------------------------------------

class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class _FakeGreeks:
    def __init__(self):
        self.impliedVol = 0.20
        self.delta = 0.42
        self.optPrice = 2.18
        self.pvDividend = 0.0
        self.gamma = 0.031
        self.vega = 0.09
        self.theta = -0.15
        self.undPrice = 658.42


class _FakeTicker:
    def __init__(self, contract):
        self.contract = contract
        self.bid = 2.10
        self.ask = 2.25
        self.last = 2.18
        self.modelGreeks = _FakeGreeks()


class _FakeContract:
    def __init__(self, symbol, expiry, strike, right):
        self.symbol = symbol
        self.lastTradeDateOrContractMonth = expiry  # 'YYYYMMDD'
        self.strike = strike
        self.right = right


class _FakeUnderlying:
    def __init__(self, symbol, exchange, sec_type):
        self.symbol = symbol
        self.exchange = exchange
        self.secType = sec_type
        self.conId = 0


class _FakeChain:
    def __init__(self, exchange="SMART"):
        self.exchange = exchange
        self.expirations = ["20260918", "20261016"]
        self.strikes = [660.0]


class _FakeIBModule:
    """Stands in for ib_insync / ib_async in tests."""

    class Stock(_FakeUnderlying):
        def __init__(self, symbol, exchange):
            super().__init__(symbol, exchange, "STK")

    class Index(_FakeUnderlying):
        def __init__(self, symbol, exchange):
            super().__init__(symbol, exchange, "IND")

    @staticmethod
    def Option(symbol, expiry, strike, right, exchange):
        return _FakeContract(symbol, expiry, strike, right)


class _FakeIB:
    """Minimal fake of the ib_insync IB object used by IBKRAdapter."""

    def __init__(self, chains=None):
        self.tickSizeEvent = _FakeEvent()
        self.tickGenericEvent = _FakeEvent()
        self.chains = [_FakeChain()] if chains is None else chains
        self.market_data_type = None
        self.disconnected = False

    def isConnected(self):
        return True

    def reqMarketDataType(self, md_type):
        self.market_data_type = md_type

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 999

    def reqSecDefOptParams(self, symbol, exchange, sec_type, con_id):
        return self.chains

    def reqMktData(self, contract, genericTickList="", snapshot=False,
                   regulatorySnapshot=False, mktDataOptions=None):
        ticker = _FakeTicker(contract)
        # Fire the documented protocol ticks: 27/28 OI, 29/30 volume, 24 IV.
        oi_tick = 27 if contract.right == "C" else 28
        vol_tick = 29 if contract.right == "C" else 30
        self.tickSizeEvent.fire(ticker, oi_tick, 1500.0)
        self.tickSizeEvent.fire(ticker, vol_tick, 42.0)
        self.tickGenericEvent.fire(ticker, 24, 0.20)
        return ticker

    def cancelMktData(self, contract):
        pass

    def sleep(self, seconds):
        pass

    def disconnect(self):
        self.disconnected = True


def _ibkr_adapter(**overrides):
    return IBKRAdapter(
        ib_factory=_FakeIB, ib_module=_FakeIBModule, **overrides
    )


class TestIBKRAdapter(unittest.TestCase):
    def test_missing_dependency_raises_clear_error(self):
        adapter = IBKRAdapter()  # no factory, no module -> must import on use
        original = ibkr_mod._load_ib
        ibkr_mod._load_ib = lambda: (_ for _ in ()).throw(
            DependencyMissingError("no ib_insync")
        )
        try:
            with self.assertRaises(DependencyMissingError):
                adapter.fetch_raw("SPY")
        finally:
            ibkr_mod._load_ib = original

    def test_settings_from_env(self):
        import os
        from unittest import mock

        env = {"IBKR_HOST": "10.0.0.5", "IBKR_PORT": "4002",
               "IBKR_CLIENT_ID": "7", "IBKR_MARKET_DATA_TYPE": "3"}
        with mock.patch.dict(os.environ, env):
            adapter = IBKRAdapter(ib_module=_FakeIBModule)
        self.assertEqual(adapter.settings["host"], "10.0.0.5")
        self.assertEqual(adapter.settings["port"], 4002)
        self.assertEqual(adapter.settings["client_id"], 7)
        self.assertEqual(adapter.settings["market_data_type"], 3)

    def test_fetch_and_parse_fake_gateway(self):
        adapter = _ibkr_adapter()
        raw = adapter.fetch_raw("SPY")
        self.assertEqual(raw["symbol"], "SPY")
        self.assertAlmostEqual(raw["spot"], 658.42)
        # 2 expiries x 1 strike x 2 rights = 4 contracts
        self.assertEqual(len(raw["contracts"]), 4)

        df = adapter.parse(raw, "SPY")
        self.assertEqual(list(df.columns), SCHEMA_COLUMNS)
        validate_schema(df)  # must not raise

        call = df[(df["expiry"] == "2026-09-18")
                  & (df["option_type"] == "call")].iloc[0]
        self.assertEqual(call["strike"], 660.0)
        self.assertAlmostEqual(call["bid"], 2.10)
        self.assertAlmostEqual(call["ask"], 2.25)
        self.assertAlmostEqual(call["last"], 2.18)
        self.assertAlmostEqual(call["implied_volatility"], 0.20)
        self.assertEqual(call["open_interest"], 1500)
        self.assertEqual(call["volume"], 42)
        self.assertAlmostEqual(call["delta"], 0.42)
        self.assertAlmostEqual(call["gamma"], 0.031)
        self.assertAlmostEqual(call["theta"], -0.15)
        self.assertAlmostEqual(call["vega"], 0.09)
        self.assertAlmostEqual(call["spot"], 658.42)

        put = df[df["option_type"] == "put"].iloc[0]
        self.assertEqual(put["open_interest"], 1500)  # tick 28 path

    def test_spx_uses_index_contract(self):
        seen = {}

        class RecordingModule(_FakeIBModule):
            class Index(_FakeIBModule.Index):
                def __init__(self, symbol, exchange):
                    seen["index"] = (symbol, exchange)
                    super().__init__(symbol, exchange)

        adapter = IBKRAdapter(ib_factory=_FakeIB, ib_module=RecordingModule)
        adapter.fetch_raw("SPX")
        self.assertEqual(seen["index"], ("SPX", "CBOE"))

    def test_no_smart_chain_raises(self):
        adapter = IBKRAdapter(
            ib_factory=lambda: _FakeIB(chains=[_FakeChain(exchange="CBOE")]),
            ib_module=_FakeIBModule,
        )
        with self.assertRaises(ChainSourceError):
            adapter.fetch_raw("SPY")

    def test_batching_respected(self):
        adapter = _ibkr_adapter(batch_size=2, pacing_delay=0)
        raw = adapter.fetch_raw("QQQ")
        self.assertEqual(len(raw["contracts"]), 4)


if __name__ == "__main__":
    unittest.main()
