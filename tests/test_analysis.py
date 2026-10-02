"""Analysis tab tests. Run: python -m unittest -v tests.test_analysis"""

import logging
import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "IIFL_ENV_FILE": os.path.join(_TMP, "none.env"),
    "BOT_DB_FILE": os.path.join(_TMP, "a.db"),
    "BOT_DATA_FILE": os.path.join(_TMP, "bots.json"),
})

import backtest  # noqa: E402
import config  # noqa: E402
import candles  # noqa: E402
import db  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
logging.disable(logging.CRITICAL)


def bar(ts, o, h=None, l=None, c=None):
    h = o if h is None else h
    l = o if l is None else l
    c = o if c is None else c
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 0}


BASE = {"target_pct": 1.0, "shares": 10, "max_open": 50, "capital_limit": None, "tick": 0.01}


class TestParser(unittest.TestCase):
    def test_list_rows_with_iso_time(self):
        body = {"status": "Ok", "result": [
            ["2026-09-30T09:15:00+05:30", 59, 59.5, 58.9, 59.2, 1000],
            ["2026-09-30T09:16:00+05:30", 59.2, 59.3, 59.1, 59.25, 500],
            ["2026-09-30T15:45:00+05:30", 1, 1, 1, 1, 1],            # outside market hours
        ]}
        out = candles.parse_candles(body)
        self.assertEqual([c["ts"] for c in out], ["2026-09-30 09:15", "2026-09-30 09:16"])
        self.assertEqual(out[0]["high"], 59.5)

    def test_epoch_ms_and_nested(self):
        epoch = int(datetime(2026, 9, 30, 9, 20, tzinfo=IST).timestamp() * 1000)
        body = {"result": [{"candles": [[epoch, "10", "11", "9", "10.5", "7"]]}]}
        self.assertEqual(candles.parse_candles(body)[0]["ts"], "2026-09-30 09:20")

    def test_dict_rows_and_strings(self):
        body = {"result": {"candles": [
            {"time": "30-Sep-2026 09:17:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            "2026-09-30 09:18:00|1|2|0.5|1.5|9",
        ]}}
        self.assertEqual([c["ts"][11:] for c in candles.parse_candles(body)], ["09:17", "09:18"])


class TestSync(unittest.TestCase):
    def setUp(self):
        db.reset_connection()
        config.BOT_DB_FILE = os.path.join(_TMP, os.path.basename(os.environ['BOT_DB_FILE']))
        config.DATA_DIR = _TMP
        db._initialized_paths.clear()
        for f in os.listdir(_TMP):
            os.remove(os.path.join(_TMP, f))
        ts = db.now_utc()
        with db.tx() as c:
            c.execute("INSERT INTO analysis_symbols(instrumentId, symbol, exchange, tick_size, live, sync_from, "
                      "sync_requested, created_at, updated_at) VALUES('6792','SBC','NSEEQ',0.01,1,'2026-09-28',1,?,?)",
                      (ts, ts))
        self.calls = []

    def fake_fetch(self, iid, exchange, d1, d2):
        self.calls.append((d1, d2))
        out, d = [], d1
        while d <= d2:
            out.append(bar(f"{d} 09:15", 59.0))
            out.append(bar(f"{d} 09:16", 59.1))
            d = date.fromordinal(d.toordinal() + 1)
        return out

    def test_requested_then_live(self):
        now = datetime(2026, 10, 5, 12, 0, tzinfo=IST)            # Monday, market open
        candles.run_requested(now, fetch=self.fake_fetch, pause=0)
        sym = db.row("SELECT * FROM analysis_symbols")
        self.assertEqual(sym["sync_requested"], 0)
        self.assertEqual(sym["last_synced_day"], "2026-10-01")    # 2 Oct holiday, 3-4 weekend
        days = [r["d"] for r in db.rows("SELECT DISTINCT substr(ts,1,10) d FROM candles ORDER BY d")]
        self.assertEqual(days, ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"])
        candles.run_live_daily(datetime(2026, 10, 5, 16, 0, tzinfo=IST), fetch=self.fake_fetch, pause=0)
        self.assertEqual(db.row("SELECT last_synced_day FROM analysis_symbols")["last_synced_day"], "2026-10-01")
        candles.run_live_daily(datetime(2026, 10, 5, 17, 5, tzinfo=IST), fetch=self.fake_fetch, pause=0)
        self.assertEqual(db.row("SELECT last_synced_day FROM analysis_symbols")["last_synced_day"], "2026-10-05")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM candles WHERE ts LIKE '2026-10-05%'"), 2)
        n = len(self.calls)
        candles.run_live_daily(datetime(2026, 10, 5, 17, 30, tzinfo=IST), fetch=self.fake_fetch, pause=0)
        self.assertEqual(len(self.calls), n)                      # once per day

    def test_chunk_failure_falls_back_to_days(self):
        def flaky(iid, ex, d1, d2):
            if d1 != d2:
                raise candles.CandleFetchError("range too large")
            return self.fake_fetch(iid, ex, d1, d2)
        candles.run_requested(datetime(2026, 10, 5, 12, 0, tzinfo=IST), fetch=flaky, pause=0)
        self.assertEqual(db.scalar("SELECT COUNT(DISTINCT substr(ts,1,10)) FROM candles"), 4)


class TestBacktest(unittest.TestCase):
    def test_entry_per_slot_and_target_exit(self):
        cs = [bar("2026-09-30 09:15", 100), bar("2026-09-30 09:16", 100, h=101.0),
              bar("2026-09-30 09:17", 100.5)]
        r = backtest.simulate(cs, {**BASE}, "1m")["summary"]
        # 09:15 buy@100 (t 101), 09:16 buy@100 (t 101) -> both sell @101 in 09:16, 09:17 buy@100.5
        self.assertEqual(r["entries"], 3)
        self.assertEqual(r["closed"], 2)
        self.assertEqual(r["net_pnl"], 20.0)
        self.assertEqual(r["open_count"], 1)

    def test_timeframes(self):
        cs = [bar(f"2026-09-30 09:{m:02d}", 100) for m in range(15, 45)]
        s = {tf: backtest.simulate(cs, BASE, tf)["summary"]["entries"] for tf in ("1m", "5m", "15m")}
        self.assertEqual(s, {"1m": 30, "5m": 6, "15m": 2})

    def test_gap_up_sells_at_open_and_target_rounding(self):
        cs = [bar("2026-09-30 09:15", 59.03), bar("2026-09-30 09:16", 60.0)]
        r = backtest.simulate(cs, {**BASE, "shares": 1}, "15m")
        t = [x for x in r["trades"] if x["status"] == "Closed"][0]
        self.assertEqual(t["target"], 59.63)
        self.assertEqual(t["exit"], 60.0)

    def test_max_open_and_capital(self):
        cs = [bar(f"2026-09-30 09:{m:02d}", 100) for m in range(15, 25)]
        r = backtest.simulate(cs, {**BASE, "max_open": 3}, "1m")["summary"]
        self.assertEqual((r["entries"], r["skipped_max_open"], r["peak_capital"]), (3, 7, 3000.0))
        r = backtest.simulate(cs, {**BASE, "capital_limit": 2500}, "1m")["summary"]
        self.assertEqual((r["entries"], r["skipped_capital"]), (2, 8))

    def test_batch_and_rebuy(self):
        cs = [bar("2026-09-30 09:15", 100), bar("2026-09-30 09:16", 100),
              bar("2026-09-30 09:17", 100, h=101),        # both sell @101 -> rebuy level 99.99 (1% dip)
              bar("2026-09-30 09:18", 100.5),            # waiting
              bar("2026-09-30 09:19", 100.2, l=99.9),    # dip -> buy @99.99
              bar("2026-09-30 09:20", 99.5)]             # next slot buy (batch of 2 complete)
        r = backtest.simulate(cs, {**BASE, "batch_size": 2, "rebuy_dip_pct": 1.0}, "1m")
        s = r["summary"]
        self.assertEqual(s["entries"], 4)
        self.assertEqual(s["batches"], 2)
        buys = sorted(t["entry"] for t in r["trades"])
        self.assertEqual(buys, [99.5, 99.99, 100, 100])

    def test_charges_and_overnight(self):
        cs = [bar("2026-09-30 09:15", 100), bar("2026-10-01 09:15", 101.5)]
        r = backtest.simulate(cs, {**BASE, "charges_pct": 0.1}, "15m")["summary"]
        # buy 100 day1; day2 open 101.5 gap-up sells first lot @101.5; day2 buy @101.5 stays open
        self.assertEqual(r["gross_pnl"], 15.0)
        self.assertEqual(r["charges"], round((100 + 101.5) * 10 * 0.001, 2))
        self.assertEqual(r["open_count"], 1)
        self.assertEqual(r["trading_days"], 2)

    def test_run_compares_all_timeframes(self):
        cs = [bar(f"2026-09-30 09:{m:02d}", 100 + (m % 3) * 0.6) for m in range(15, 59)]
        out = backtest.run(cs, {**BASE, "timeframe": "5m"})
        self.assertEqual(set(out["compare"]), {"1m", "5m", "15m"})
        self.assertEqual(out["result"]["summary"]["timeframe"], "5m")


if __name__ == "__main__":
    unittest.main()
