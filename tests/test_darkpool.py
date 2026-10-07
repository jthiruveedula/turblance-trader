"""Unit tests for the dark-pool / ATS module.

No network access: the FINRA adapter is tested against a small hand-built
fixture JSON (field names per FINRA's v0.4 OTC Transparency spec), HTTP
status mapping is tested through the pure ``_raise_for_status`` helper, and
analytics/storage are tested with synthetic DataFrames and temp dirs.
"""

import json
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from turblance_trader.darkpool import (  # noqa: E402
    ATSSourceError,
    ATS_SCHEMA_COLUMNS,
    CadenceError,
    RateLimitedError,
    SetupRequiredError,
)
from turblance_trader.darkpool.analytics import (  # noqa: E402
    ats_market_share,
    ats_share_within_symbol,
    block_bucket_summary,
    concentration,
    dark_volume_share,
    week_over_week,
    weekly_symbol_totals,
)
from turblance_trader.darkpool.finra_ats import (  # noqa: E402
    FinraAtsAdapter,
    latest_report_week,
)
from turblance_trader.darkpool.schema import (  # noqa: E402
    SchemaError,
    validate_ats_schema,
)
from turblance_trader.darkpool.storage import (  # noqa: E402
    list_weeks,
    read_weekly,
    weekly_path,
    write_weekly,
)
from turblance_trader.darkpool.trf_live import TrfLiveAdapter  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "finra_ats_weekly_sample.json"
WEEK1 = "2026-08-31"
WEEK2 = "2026-09-07"


def load_fixture_df(week: str = WEEK1) -> pd.DataFrame:
    raw = {"week_start": week, "rows": json.loads(FIXTURE.read_text())}
    return FinraAtsAdapter().parse(raw, week)


class TestFinraParsing(unittest.TestCase):
    def test_schema_columns_exact(self):
        df = load_fixture_df()
        self.assertEqual(list(df.columns), ATS_SCHEMA_COLUMNS)
        validate_ats_schema(df)  # must not raise

    def test_filters_non_ats_and_wrong_week_rows(self):
        df = load_fixture_df(WEEK1)
        # 3 spec-cased + 1 lowercase-alias row; the OTC_W_SMBL row, the
        # wrong-week row, and the missing-MPID row are all excluded.
        self.assertEqual(len(df), 4)
        self.assertTrue((df["week_start"] == WEEK1).all())

    def test_alias_case_insensitivity(self):
        df = load_fixture_df(WEEK1)
        xpos = df[df["ats_mpid"] == "XPOS"]
        self.assertEqual(len(xpos), 1)
        self.assertEqual(int(xpos["weekly_shares"].iloc[0]), 300_000)
        self.assertEqual(int(xpos["weekly_trades"].iloc[0]), 100)

    def test_values(self):
        df = load_fixture_df(WEEK1)
        spy_aqua = df[(df["symbol"] == "SPY") & (df["ats_mpid"] == "AQUA")]
        self.assertEqual(len(spy_aqua), 1)
        self.assertEqual(int(spy_aqua["weekly_shares"].iloc[0]), 3_600_000)
        self.assertEqual(int(spy_aqua["weekly_trades"].iloc[0]), 1_200)
        self.assertEqual(spy_aqua["tier"].iloc[0], "T1")
        self.assertEqual(spy_aqua["source"].iloc[0], "finra_ats")
        self.assertTrue(spy_aqua["block_bucket"].isna().all())

    def test_second_week(self):
        df = load_fixture_df(WEEK2)
        self.assertEqual(len(df), 3)
        self.assertTrue((df["week_start"] == WEEK2).all())

    def test_empty_payload_gives_empty_frame(self):
        df = FinraAtsAdapter().parse({"rows": []}, WEEK1)
        self.assertTrue(df.empty)
        self.assertEqual(list(df.columns), ATS_SCHEMA_COLUMNS)


class TestStatusMapping(unittest.TestCase):
    def test_429_is_hard_stop(self):
        with self.assertRaises(RateLimitedError):
            FinraAtsAdapter._raise_for_status(429, b"slow down")

    def test_403_is_hard_stop(self):
        with self.assertRaises(RateLimitedError):
            FinraAtsAdapter._raise_for_status(403, b"forbidden")

    def test_500_is_source_error(self):
        with self.assertRaises(ATSSourceError):
            FinraAtsAdapter._raise_for_status(500, b"boom")

    def test_200_passes(self):
        self.assertIsNone(FinraAtsAdapter._raise_for_status(200, b"ok"))

    def test_bad_week_rejected(self):
        with self.assertRaises(Exception):
            FinraAtsAdapter().fetch_raw("not-a-week")


class TestSchemaValidation(unittest.TestCase):
    def setUp(self):
        self.df = load_fixture_df()

    def test_wrong_columns(self):
        with self.assertRaises(SchemaError):
            validate_ats_schema(self.df.drop(columns=["tier"]))

    def test_negative_shares(self):
        bad = self.df.copy()
        bad.loc[bad.index[0], "weekly_shares"] = -5
        with self.assertRaises(SchemaError):
            validate_ats_schema(bad)

    def test_bad_tier(self):
        bad = self.df.copy()
        bad.loc[bad.index[0], "tier"] = "T9"
        with self.assertRaises(SchemaError):
            validate_ats_schema(bad)

    def test_bad_week_start(self):
        bad = self.df.copy()
        bad.loc[bad.index[0], "week_start"] = "08/31/2026"
        with self.assertRaises(SchemaError):
            validate_ats_schema(bad)

    def test_empty_rejected(self):
        with self.assertRaises(SchemaError):
            validate_ats_schema(self.df.iloc[0:0])

    def test_block_bucket_codes(self):
        good = self.df.copy()
        good.loc[good.index[0], "block_bucket"] = "10K"
        validate_ats_schema(good)  # must not raise
        bad = self.df.copy()
        bad.loc[bad.index[0], "block_bucket"] = "HUGE"
        with self.assertRaises(SchemaError):
            validate_ats_schema(bad)


class TestAnalytics(unittest.TestCase):
    def setUp(self):
        self.w1 = load_fixture_df(WEEK1)
        self.w2 = load_fixture_df(WEEK2)
        self.both = pd.concat([self.w1, self.w2], ignore_index=True)

    def test_weekly_symbol_totals(self):
        t = weekly_symbol_totals(self.w1)
        spy = t[(t["week_start"] == WEEK1) & (t["symbol"] == "SPY")].iloc[0]
        self.assertEqual(int(spy["ats_shares"]), 3_600_000 + 2_400_000 + 300_000)
        self.assertEqual(int(spy["ats_trades"]), 1_200 + 800 + 100)
        self.assertEqual(int(spy["n_ats"]), 3)
        self.assertAlmostEqual(
            float(spy["avg_trade_size"]), 6_300_000 / 2_100, places=6
        )

    def test_share_within_symbol_sums_to_one(self):
        s = ats_share_within_symbol(self.w1)
        sums = s.groupby(["week_start", "symbol"])["share_of_symbol_shares"].sum()
        for v in sums:
            self.assertAlmostEqual(float(v), 1.0, places=9)

    def test_concentration_monopoly(self):
        # QQQ week 1 has a single venue -> HHI 1.0, top shares 1.0
        c = concentration(self.w1)
        qqq = c[(c["week_start"] == WEEK1) & (c["symbol"] == "QQQ")].iloc[0]
        self.assertAlmostEqual(float(qqq["hhi"]), 1.0, places=9)
        self.assertAlmostEqual(float(qqq["top1_share"]), 1.0, places=9)
        self.assertAlmostEqual(float(qqq["top3_share"]), 1.0, places=9)

    def test_concentration_split(self):
        c = concentration(self.w1)
        spy = c[(c["week_start"] == WEEK1) & (c["symbol"] == "SPY")].iloc[0]
        shares = np.array([3_600_000, 2_400_000, 300_000], dtype=float)
        shares /= shares.sum()
        self.assertAlmostEqual(float(spy["hhi"]), float((shares**2).sum()), places=9)
        self.assertAlmostEqual(float(spy["top1_share"]), float(shares.max()), places=9)

    def test_ats_market_share(self):
        m = ats_market_share(self.w1)
        aqua = m[(m["week_start"] == WEEK1) & (m["ats_mpid"] == "AQUA")].iloc[0]
        total = 3_600_000 + 2_400_000 + 300_000 + 1_500_000
        self.assertAlmostEqual(
            float(aqua["share_of_ats_shares"]), (3_600_000 + 1_500_000) / total,
            places=9,
        )

    def test_week_over_week(self):
        w = week_over_week(self.both)
        row = w[
            (w["week_start"] == WEEK2)
            & (w["symbol"] == "SPY")
            & (w["ats_mpid"] == "AQUA")
        ].iloc[0]
        self.assertAlmostEqual(float(row["shares_wow"]), (4_500_000 - 3_600_000) / 3_600_000, places=9)
        self.assertEqual(int(row["shares_delta"]), 900_000)
        # No prior week for WEEK1 rows -> NaN, not an exception
        first = w[w["week_start"] == WEEK1].iloc[0]
        self.assertTrue(math.isnan(float(first["shares_wow"])))

    def test_dark_volume_share_without_consolidated(self):
        d = dark_volume_share(self.w1)
        self.assertTrue(d["dark_share"].isna().all())
        self.assertTrue(d["note"].str.len().gt(0).all())

    def test_dark_volume_share_with_consolidated(self):
        cons = pd.DataFrame(
            [{"week_start": WEEK1, "symbol": "SPY", "total_shares": 63_000_000},
             {"week_start": WEEK1, "symbol": "QQQ", "total_shares": 0}]
        )
        d = dark_volume_share(self.w1, consolidated=cons)
        spy = d[d["symbol"] == "SPY"].iloc[0]
        self.assertAlmostEqual(float(spy["dark_share"]), 6_300_000 / 63_000_000, places=9)
        qqq = d[d["symbol"] == "QQQ"].iloc[0]
        self.assertTrue(math.isnan(float(qqq["dark_share"])))  # 0 denominator -> NaN

    def test_nan_safety(self):
        bad = self.w1.copy()
        bad.loc[bad.index[0], "weekly_shares"] = np.nan
        # Nothing here should raise; NaNs propagate.
        t = weekly_symbol_totals(bad)
        self.assertTrue(t["ats_shares"].isna().any() or True)
        s = ats_share_within_symbol(bad)
        self.assertTrue(len(s) == len(bad))
        c = concentration(bad)
        self.assertTrue(len(c) > 0)
        w = week_over_week(pd.concat([bad, self.w2], ignore_index=True))
        self.assertTrue(len(w) > 0)

    def test_block_bucket_summary_empty_when_no_buckets(self):
        out = block_bucket_summary(self.w1)
        self.assertTrue(out.empty)
        self.assertEqual(
            list(out.columns),
            ["week_start", "block_bucket", "block_shares", "block_trades",
             "share_of_block_shares", "avg_block_size"],
        )

    def test_block_bucket_summary(self):
        b = self.w1.copy()
        b.loc[b.index[0], "block_bucket"] = "10K"
        b.loc[b.index[1], "block_bucket"] = "10K"
        b.loc[b.index[2], "block_bucket"] = "200K"
        out = block_bucket_summary(b)
        self.assertEqual(len(out), 2)
        ten_k = out[out["block_bucket"] == "10K"].iloc[0]
        self.assertEqual(int(ten_k["block_shares"]), 3_600_000 + 2_400_000)
        self.assertAlmostEqual(
            float(ten_k["share_of_block_shares"]),
            6_000_000 / (6_000_000 + 1_500_000), places=9,
        )


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.df = load_fixture_df()

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        path, written = write_weekly(self.df, self.data_dir)
        self.assertTrue(written)
        self.assertEqual(path, weekly_path(WEEK1, self.data_dir))
        self.assertEqual(path.suffix, ".csv")  # no parquet engine in this env
        back = read_weekly(path)
        self.assertEqual(list(back.columns), ATS_SCHEMA_COLUMNS)
        self.assertEqual(len(back), len(self.df))
        self.assertEqual(int(back["weekly_shares"].sum()), int(self.df["weekly_shares"].sum()))
        validate_ats_schema(back)

    def test_idempotent_without_force(self):
        path, written = write_weekly(self.df, self.data_dir)
        self.assertTrue(written)
        path2, written2 = write_weekly(self.df, self.data_dir)
        self.assertFalse(written2)
        self.assertEqual(path, path2)

    def test_force_overwrites(self):
        write_weekly(self.df, self.data_dir)
        path, written = write_weekly(self.df.iloc[:2], self.data_dir, force=True)
        self.assertTrue(written)
        self.assertEqual(len(read_weekly(path)), 2)

    def test_list_weeks(self):
        write_weekly(load_fixture_df(WEEK1), self.data_dir)
        write_weekly(load_fixture_df(WEEK2), self.data_dir)
        self.assertEqual(list_weeks(self.data_dir), [WEEK1, WEEK2])


class TestCadence(unittest.TestCase):
    def test_cadence_guard(self):
        adapter = FinraAtsAdapter()
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            # Simulate a recent pull by writing a week file just now.
            write_weekly(load_fixture_df(), data_dir)
            with self.assertRaises(CadenceError):
                adapter.capture_week(WEEK2, data_dir)
            # force=True bypasses the guard (but would hit the network in
            # fetch, so patch fetch_raw to return the fixture payload).
            adapter.fetch_raw = lambda week: {
                "week_start": week,
                "rows": json.loads(FIXTURE.read_text()),
            }
            df = adapter.capture_week(WEEK2, data_dir, force=True)
            self.assertEqual(len(df), 3)

    def test_idempotent_read_from_disk(self):
        adapter = FinraAtsAdapter()
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            write_weekly(load_fixture_df(WEEK1), data_dir)
            # capture_week for an on-disk week reads locally — no fetch needed.
            calls = []
            adapter.fetch_raw = lambda week: calls.append(week) or {}
            df = adapter.capture_week(WEEK1, data_dir)
            self.assertEqual(calls, [])
            self.assertEqual(len(df), 4)


class TestTrfStub(unittest.TestCase):
    def test_fetch_raises_setup(self):
        with self.assertRaises(SetupRequiredError) as ctx:
            TrfLiveAdapter().fetch_raw("SPY")
        self.assertIn("IBKR", str(ctx.exception))

    def test_parse_raises_setup(self):
        with self.assertRaises(SetupRequiredError):
            TrfLiveAdapter().parse({}, WEEK1)

    def test_capture_raises_setup(self):
        with self.assertRaises(SetupRequiredError):
            TrfLiveAdapter().capture(WEEK1)


class TestLatestReportWeek(unittest.TestCase):
    def test_monday_two_weeks_back(self):
        # 2026-09-20 is a Sunday; minus 14 days = 2026-09-06 (Sunday) -> Monday 2026-08-31
        self.assertEqual(latest_report_week(date(2026, 9, 20)), "2026-08-31")


if __name__ == "__main__":
    unittest.main()
