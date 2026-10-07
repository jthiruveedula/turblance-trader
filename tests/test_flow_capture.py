"""Unit tests for the flow-capture layer (no network, no live gateway).

Covers:
  * flow-delta derivation: volume/OI diffs, new/expired contracts,
    corporate-action-ish jumps *flagged* (never silently absorbed),
    session-volume resets flagged
  * delta schema validation + idempotent storage + day-level derivation
  * IBKR tick-print path: clean no-op without gateway/dependency, fake-event
    capture, tick schema validation + storage
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.capture.adapters import ibkr as ibkr_mod  # noqa: E402
from turblance_trader.capture.adapters.ibkr import (  # noqa: E402
    DependencyMissingError,
    IBKRAdapter,
)
from turblance_trader.capture.flow import (  # noqa: E402
    DELTA_COLUMNS,
    TICK_COLUMNS,
    DeltaSchemaError,
    TickSchemaError,
    delta_path,
    derive_day_deltas,
    derive_deltas,
    tick_path,
    validate_delta_schema,
    validate_tick_schema,
    write_deltas,
    write_tick_prints,
)
from turblance_trader.capture.schema import SCHEMA_COLUMNS  # noqa: E402
from turblance_trader.capture.store import write_snapshot  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_snapshot_row(expiry, strike, option_type, volume, oi,
                      quote_time, symbol="SPY"):
    return {
        "symbol": symbol,
        "quote_time": quote_time,
        "expiry": expiry,
        "strike": float(strike),
        "option_type": option_type,
        "bid": 2.10,
        "ask": 2.25,
        "last": 2.18,
        "implied_volatility": 0.20,
        "open_interest": int(oi),
        "volume": volume,
        "delta": 0.42,
        "gamma": 0.031,
        "theta": -0.15,
        "vega": 0.09,
        "spot": 658.42,
    }


def make_snapshot(rows, quote_time):
    df = pd.DataFrame([dict(r, quote_time=quote_time) for r in rows])
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")
    df["open_interest"] = df["open_interest"].astype("Int64")
    return df[SCHEMA_COLUMNS]


QT1 = "2026-09-21T14:00:00+00:00"
QT2 = "2026-09-21T15:00:00+00:00"

ROW_A = make_snapshot_row("2026-09-25", 660.0, "call", 100, 1500, QT1)
ROW_B = make_snapshot_row("2026-09-25", 660.0, "put", 50, 800, QT1)


# ---------------------------------------------------------------------------
# flow deltas
# ---------------------------------------------------------------------------

class TestDeriveDeltas(unittest.TestCase):
    def test_volume_and_oi_diffs(self):
        prev = make_snapshot([ROW_A, ROW_B], QT1)
        curr_rows = [
            make_snapshot_row("2026-09-25", 660.0, "call", 160, 1700, QT2),
            make_snapshot_row("2026-09-25", 660.0, "put", 50, 800, QT2),
        ]
        curr = make_snapshot(curr_rows, QT2)
        df = derive_deltas(prev, curr)
        self.assertEqual(list(df.columns), DELTA_COLUMNS)

        call = df[(df["strike"] == 660.0)
                  & (df["option_type"] == "call")].iloc[0]
        self.assertEqual(call["status"], "ok")
        self.assertEqual(call["flag"], "")
        self.assertEqual(call["volume_delta"], 60)
        self.assertEqual(call["open_interest_delta"], 200)
        self.assertEqual(call["volume_prev"], 100)
        self.assertEqual(call["volume_curr"], 160)
        self.assertEqual(call["prev_quote_time"], QT1)
        self.assertEqual(call["curr_quote_time"], QT2)

        put = df[df["option_type"] == "put"].iloc[0]
        self.assertEqual(put["volume_delta"], 0)
        self.assertEqual(put["open_interest_delta"], 0)

    def test_new_contract_marked_not_diffed(self):
        prev = make_snapshot([ROW_A], QT1)
        curr = make_snapshot([ROW_A, ROW_B], QT2)
        df = derive_deltas(prev, curr)
        new = df[(df["strike"] == 660.0)
                 & (df["option_type"] == "put")].iloc[0]
        self.assertEqual(new["status"], "new")
        self.assertTrue(pd.isna(new["volume_delta"]))
        self.assertTrue(pd.isna(new["volume_prev"]))
        self.assertEqual(new["open_interest_curr"], 800)
        self.assertEqual(new["open_interest_delta"], 800)

    def test_expired_contract_marked(self):
        prev = make_snapshot([ROW_A, ROW_B], QT1)
        curr = make_snapshot([ROW_A], QT2)
        df = derive_deltas(prev, curr)
        expired = df[(df["strike"] == 660.0)
                     & (df["option_type"] == "put")].iloc[0]
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(pd.isna(expired["volume_curr"]))
        self.assertEqual(expired["open_interest_delta"], -800)

    def test_oi_jump_flagged_not_absorbed(self):
        # 100 -> 5000 OI in one interval: re-listing/adjustment pattern.
        prev = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 100, 100, QT1)], QT1)
        curr = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 120, 5000, QT2)], QT2)
        df = derive_deltas(prev, curr)
        row = df.iloc[0]
        self.assertEqual(row["flag"], "oi_jump")
        # The raw delta is still recorded — the flag marks it, it is not
        # silently folded into flow.
        self.assertEqual(row["open_interest_delta"], 4900)

    def test_small_oi_growth_not_flagged(self):
        prev = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 100, 1500, QT1)], QT1)
        curr = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 120, 2000, QT2)], QT2)
        df = derive_deltas(prev, curr)
        self.assertEqual(df.iloc[0]["flag"], "")

    def test_volume_reset_flagged(self):
        # Session volume must not decrease; a drop means session boundary /
        # re-listing / bad print.
        prev = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 500, 1500, QT1)], QT1)
        curr = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 10, 1500, QT2)], QT2)
        df = derive_deltas(prev, curr)
        row = df.iloc[0]
        self.assertEqual(row["flag"], "volume_reset")
        self.assertEqual(row["volume_delta"], -490)

    def test_missing_volume_gives_null_delta(self):
        prev = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", None, 1500, QT1)], QT1)
        curr = make_snapshot([make_snapshot_row(
            "2026-09-25", 660.0, "call", 40, 1500, QT2)], QT2)
        df = derive_deltas(prev, curr)
        self.assertTrue(pd.isna(df.iloc[0]["volume_delta"]))


class TestDeltaSchema(unittest.TestCase):
    def test_validates_clean_frame(self):
        prev = make_snapshot([ROW_A], QT1)
        curr = make_snapshot([ROW_A], QT2)
        validate_delta_schema(derive_deltas(prev, curr))  # must not raise

    def test_rejects_bad_status(self):
        prev = make_snapshot([ROW_A], QT1)
        curr = make_snapshot([ROW_A], QT2)
        df = derive_deltas(prev, curr)
        df.loc[df.index[0], "status"] = "mystery"
        with self.assertRaises(DeltaSchemaError):
            validate_delta_schema(df)

    def test_rejects_bad_flag(self):
        prev = make_snapshot([ROW_A], QT1)
        curr = make_snapshot([ROW_A], QT2)
        df = derive_deltas(prev, curr)
        df.loc[df.index[0], "flag"] = "suspicious"
        with self.assertRaises(DeltaSchemaError):
            validate_delta_schema(df)

    def test_rejects_wrong_columns(self):
        prev = make_snapshot([ROW_A], QT1)
        curr = make_snapshot([ROW_A], QT2)
        df = derive_deltas(prev, curr).drop(columns=["flag"])
        with self.assertRaises(DeltaSchemaError):
            validate_delta_schema(df)


class TestDeltaStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = make_snapshot([ROW_A, ROW_B], QT1)
        curr_rows = [
            make_snapshot_row("2026-09-25", 660.0, "call", 160, 1700, QT2),
            make_snapshot_row("2026-09-25", 660.0, "put", 70, 900, QT2),
        ]
        self.curr = make_snapshot(curr_rows, QT2)
        self.deltas = derive_deltas(self.prev, self.curr)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delta_path_layout(self):
        path = delta_path("SPY", QT1, QT2, self.tmp.name)
        rel = path.relative_to(self.tmp.name)
        self.assertEqual(
            str(rel),
            "flow/deltas/SPY/2026-09-21/deltas_140000_to_150000_utc.csv",
        )

    def test_write_idempotent(self):
        p1, w1 = write_deltas(self.deltas, self.tmp.name)
        p2, w2 = write_deltas(self.deltas, self.tmp.name)
        self.assertTrue(w1)
        self.assertFalse(w2)
        self.assertEqual(p1, p2)
        self.assertTrue(p1.exists())

    def test_derive_day_deltas_from_banked_snapshots(self):
        # Two snapshots banked by the chain cron -> one delta file, no network.
        write_snapshot(self.prev, self.tmp.name)
        write_snapshot(self.curr, self.tmp.name)
        out = derive_day_deltas("SPY", "2026-09-21", self.tmp.name)
        self.assertEqual(len(out), 1)
        path, written = out[0]
        self.assertTrue(written)
        self.assertTrue(str(path).startswith(
            str(Path(self.tmp.name) / "flow" / "deltas" / "SPY")))
        back = pd.read_csv(path)
        self.assertEqual(list(back.columns), DELTA_COLUMNS)
        self.assertEqual(len(back), 2)
        # Second run derives nothing new (idempotent).
        out2 = derive_day_deltas("SPY", "2026-09-21", self.tmp.name)
        self.assertEqual(len(out2), 1)
        self.assertFalse(out2[0][1])

    def test_derive_day_deltas_single_snapshot_is_noop(self):
        write_snapshot(self.prev, self.tmp.name)
        self.assertEqual(
            derive_day_deltas("SPY", "2026-09-21", self.tmp.name), [])

    def test_derive_day_deltas_no_snapshots_is_noop(self):
        self.assertEqual(
            derive_day_deltas("SPY", "2026-09-21", self.tmp.name), [])


# ---------------------------------------------------------------------------
# IBKR tick-print path (fakes — no live gateway, no ib_insync)
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


class _FakeTick:
    def __init__(self, price, size, when):
        self.price = price
        self.size = size
        self.time = when


class _FakeContract:
    def __init__(self, expiry_yyyymmdd, strike, right):
        self.lastTradeDateOrContractMonth = expiry_yyyymmdd
        self.strike = strike
        self.right = right


class _FakeTickTicker:
    def __init__(self, contract):
        self.contract = contract


class _FakeTickIB:
    """Fake gateway speaking the tick-by-tick event protocol."""

    def __init__(self, connected=True):
        self.tickByTickAllLastEvent = _FakeEvent()
        self.connected = connected

    def isConnected(self):
        return self.connected

    def reqMarketDataType(self, md_type):
        pass

    def reqTickByTickData(self, contract, tickType="Last"):
        ticker = _FakeTickTicker(contract)
        # Two prints arrive synchronously, as with a real streaming callback.
        self.tickByTickAllLastEvent.fire(
            ticker, _FakeTick(2.20, 5, datetime(2026, 9, 21, 14, 5, 1,
                                               tzinfo=timezone.utc)))
        self.tickByTickAllLastEvent.fire(
            ticker, _FakeTick(2.22, 3, datetime(2026, 9, 21, 14, 5, 7,
                                               tzinfo=timezone.utc)))
        return ticker

    def cancelTickByTickData(self, ticker):
        pass

    def sleep(self, seconds):
        pass

    def disconnect(self):
        pass


class _FakeIBModule:
    pass  # only needs to exist; discovery is bypassed with explicit contracts


def _tick_adapter(**overrides):
    contracts = [_FakeContract("20260925", 660.0, "C")]
    adapter = IBKRAdapter(
        ib_factory=lambda: _FakeTickIB(),
        ib_module=_FakeIBModule,
        **overrides,
    )
    return adapter, contracts


class TestTickCaptureNoGateway(unittest.TestCase):
    def test_no_ib_dependency_noops(self):
        adapter = IBKRAdapter(ib_factory=lambda: _FakeTickIB())  # no ib_module
        contracts = [_FakeContract("20260925", 660.0, "C")]
        original = ibkr_mod._load_ib
        ibkr_mod._load_ib = lambda: (_ for _ in ()).throw(
            DependencyMissingError("no ib_insync")
        )
        try:
            self.assertIsNone(adapter.fetch_ticks("SPY", contracts))
        finally:
            ibkr_mod._load_ib = original

    def test_unreachable_gateway_noops(self):
        adapter = IBKRAdapter(
            ib_factory=lambda: _FakeTickIB(connected=False),
            ib_module=_FakeIBModule,
        )
        contracts = [_FakeContract("20260925", 660.0, "C")]
        self.assertIsNone(adapter.fetch_ticks("SPY", contracts))

    def test_bad_tick_type_rejected(self):
        adapter, contracts = _tick_adapter()
        with self.assertRaises(ValueError):
            adapter.fetch_ticks("SPY", contracts, tick_type="BidAsk")


class TestTickCaptureFakeGateway(unittest.TestCase):
    def test_prints_captured_into_schema(self):
        adapter, contracts = _tick_adapter()
        df = adapter.fetch_ticks("SPY", contracts, tick_type="Last")
        self.assertIsNotNone(df)
        self.assertEqual(list(df.columns), TICK_COLUMNS)
        validate_tick_schema(df)  # must not raise
        self.assertEqual(len(df), 2)
        self.assertTrue((df["symbol"] == "SPY").all())
        self.assertTrue((df["option_type"] == "call").all())
        self.assertTrue((df["expiry"] == "2026-09-25").all())
        self.assertEqual(list(df["price"]), [2.20, 2.22])
        self.assertEqual(list(df["size"].astype(int)), [5, 3])
        self.assertTrue((df["tick_type"] == "Last").all())
        self.assertEqual(df["tick_time"].iloc[0],
                         "2026-09-21T14:05:01+00:00")

    def test_handlers_detached_after_capture(self):
        adapter, contracts = _tick_adapter()
        ib = _FakeTickIB()
        with mock.patch.object(adapter, "_connect", return_value=ib):
            adapter.fetch_ticks("SPY", contracts)
        self.assertEqual(ib.tickByTickAllLastEvent.handlers, [])

    def test_alllast_supported(self):
        adapter, contracts = _tick_adapter()
        df = adapter.fetch_ticks("SPY", contracts, tick_type="AllLast")
        self.assertIsNotNone(df)
        self.assertTrue((df["tick_type"] == "AllLast").all())


class TestTickStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        adapter, contracts = _tick_adapter()
        self.df = adapter.fetch_ticks("SPY", contracts)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tick_path_layout(self):
        day = pd.Timestamp(self.df["capture_time"].iloc[0], tz="UTC").strftime(
            "%Y-%m-%d")
        path = tick_path("SPY", self.df["capture_time"].iloc[0], self.tmp.name)
        rel = path.relative_to(self.tmp.name)
        self.assertTrue(str(rel).startswith(f"flow/ticks/SPY/{day}/ticks_"))
        self.assertTrue(str(rel).endswith("_utc.csv"))

    def test_write_idempotent(self):
        p1, w1 = write_tick_prints(self.df, self.tmp.name)
        p2, w2 = write_tick_prints(self.df, self.tmp.name)
        self.assertTrue(w1)
        self.assertFalse(w2)
        self.assertEqual(p1, p2)
        back = pd.read_csv(p1)
        self.assertEqual(list(back.columns), TICK_COLUMNS)
        self.assertEqual(len(back), 2)

    def test_tick_schema_rejects_bad_tick_type(self):
        df = self.df.copy()
        df.loc[df.index[0], "tick_type"] = "Weird"
        with self.assertRaises(TickSchemaError):
            validate_tick_schema(df)


if __name__ == "__main__":
    unittest.main()
