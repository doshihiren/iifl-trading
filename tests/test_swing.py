"""Swing Scanner tests. Run: python -m unittest -v tests.test_swing"""

import logging
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "IIFL_ENV_FILE": os.path.join(_TMP, "none.env"),
    "BOT_DB_FILE": os.path.join(_TMP, "s.db"),
    "BOT_DATA_FILE": os.path.join(_TMP, "bots.json"),
})

import broker  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import swing  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
logging.disable(logging.CRITICAL)

CONTRACTS = [
    {"instrumentId": "1", "tradingSymbol": "AAA-EQ", "tickSize": "0.05"},
    {"instrumentId": "2", "tradingSymbol": "BBB-EQ"},
    {"instrumentId": "3", "tradingSymbol": "CCC-BE"},          # not EQ series -> skipped
]


def days(n, start=date(2024, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def series(closes, start=date(2024, 1, 1), vol=1_000_000):
    """Rows (d, o, h, l, c, v): open = previous close, high/low ±0.5%."""
    ds = days(len(closes), start)
    rows, prev = [], closes[0]
    for d, c in zip(ds, closes):
        o = prev
        rows.append((d, o, max(o, c) * 1.005, min(o, c) * 0.995, c, vol))
        prev = c
    return rows


def uptrend_with_dip(n_up=230, dip=6, rec=12, step=0.02):
    closes = [100 + i * 0.5 for i in range(n_up)]                  # steady up-trend, RSI high
    top = closes[-1]
    closes += [top * (1 - step * (k + 1)) for k in range(dip)]     # sharp pull-back, RSI < 30
    low = closes[-1]
    closes += [low * (1 + 0.015 * (k + 1)) for k in range(rec)]    # recovery, RSI back above 40
    return closes


class Base(unittest.TestCase):
    def setUp(self):
        db.reset_connection()
        config.BOT_DB_FILE = os.environ["BOT_DB_FILE"]
        config.DATA_DIR = _TMP
        config.HOLIDAYS_FILE = os.path.join(_TMP, "holidays.json")
        db._initialized_paths.clear()
        for f in os.listdir(_TMP):
            os.remove(os.path.join(_TMP, f))
        db.init_db()
        swing.clear_cache()
        self.p = swing.params({"min_value_cr": 0, "min_price": 0})

    def put(self, iid, symbol, rows):
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO swing_universe(instrumentId, symbol, tick_size) VALUES(?,?,0.05)", (iid, symbol))
            c.executemany("INSERT INTO daily_candles VALUES(?,?,?,?,?,?,?)", [(iid, *r) for r in rows])


class TestIndicators(unittest.TestCase):
    def test_sma_and_rsi(self):
        self.assertEqual(swing.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        up = swing.rsi([float(i) for i in range(30)], 10)
        self.assertIsNone(up[9])
        self.assertEqual(up[10], 100.0)
        down = swing.rsi([float(100 - i) for i in range(30)], 10)
        self.assertEqual(round(down[20]), 0)

    def test_params_validation(self):
        with self.assertRaises(ValueError):
            swing.params({"entry_rsi": 50, "exit_rsi": 40})
        p = swing.params({"max_positions": "3", "sl_mode": "bogus"})
        self.assertEqual((p["max_positions"], p["sl_mode"]), (3, "none"))


class TestSync(Base):
    def test_history_then_daily_topup_and_split_on_error(self):
        calls = []

        def fetch(iid, a, b):
            calls.append((iid, a, b))
            if (b - a).days > 200:
                raise RuntimeError("range too large")
            return [{"d": d, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}
                    for d in days(400, date(2023, 1, 2)) if a.isoformat() <= d <= b.isoformat()]

        swing.request_download(1)
        now = datetime(2024, 3, 6, 18, 0, tzinfo=IST)             # Wednesday evening
        self.assertEqual(swing.run_sync(now, fetch=fetch, pause=0, contracts=CONTRACTS), "done")
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM swing_universe"), 2)
        self.assertEqual(db.scalar("SELECT MAX(d) FROM daily_candles"), "2024-03-06")
        self.assertGreater(db.scalar("SELECT COUNT(*) FROM daily_candles WHERE instrumentId='1'"), 240)
        self.assertEqual(swing.get_state()["state"], "DONE")
        self.assertIsNone(swing.run_sync(now, fetch=fetch, pause=0))        # nothing more today
        n = len(calls)
        swing.run_sync(datetime(2024, 3, 7, 17, 0, tzinfo=IST), fetch=fetch, pause=0)
        self.assertEqual(len(calls), n)                                     # before 17:30: wait
        swing.run_sync(datetime(2024, 3, 7, 17, 45, tzinfo=IST), fetch=fetch, pause=0)
        self.assertEqual([c[1] for c in calls[n:]], [date(2024, 3, 7)] * 2)  # only the new day
        self.assertEqual(db.scalar("SELECT MAX(d) FROM daily_candles"), "2024-03-07")

    def test_session_expiry_pauses_and_resumes(self):
        def expired(iid, a, b):
            raise broker.SessionExpired("expired")
        swing.request_download(1)
        now = datetime(2024, 3, 6, 18, 0, tzinfo=IST)
        self.assertEqual(swing.run_sync(now, fetch=expired, pause=0, contracts=CONTRACTS), "session")
        st = swing.get_state()
        self.assertEqual(st["state"], "WAITING_LOGIN")
        self.assertTrue(st["requested"])                       # retried once logged in
        ok = swing.run_sync(now, fetch=lambda i, a, b: [], pause=0, contracts=CONTRACTS)
        self.assertEqual(ok, "done")

    def test_parse_daily_keeps_date(self):
        body = {"result": [["2026-09-30T00:00:00+05:30", 10, 11, 9, 10.5, 1000],
                           ["2026-10-01T00:00:00+05:30", 10.5, 11, 10, 10.8, 900]]}
        self.assertEqual([r["d"] for r in swing.parse_daily(body)], ["2026-09-30", "2026-10-01"])


class TestStrategy(Base):
    def setUp(self):
        super().setUp()
        self.put("1", "AAA", series(uptrend_with_dip()))
        self.stocks = swing.load_stocks(self.p)
        self.s = self.stocks[0]

    def signal_day(self):
        return next(i for i in range(len(self.s.days)) if self.s.setup(i, self.p))

    def test_buy_next_open_sell_next_open_after_rsi_exit(self):
        res = swing.backtest(self.stocks, {**self.p, "max_positions": 1})
        t = res["trades"][0]
        i = self.signal_day()
        self.assertEqual(t["entry_day"], self.s.days[i + 1])
        self.assertAlmostEqual(t["entry"], round(self.s.o[i + 1], 2))
        exit_i = self.s.pos[t["exit_day"]]
        self.assertGreater(self.s.rsi[exit_i - 1], 40)          # signal at previous close
        self.assertAlmostEqual(t["exit"], round(self.s.o[exit_i], 2))
        self.assertTrue(t["reason"].startswith("RSI"))
        self.assertEqual(res["summary"]["trades"], len(res["trades"]))

    def test_time_exit(self):
        res = swing.backtest(self.stocks, {**self.p, "exit_rsi": 99, "max_hold": 3})
        t = res["trades"][0]
        self.assertEqual(t["days"], 4)                             # held 3 days, sold the 4th morning
        self.assertIn("3 days", t["reason"])

    def test_stop_loss_pct(self):
        i = self.signal_day()
        # make the day after entry crash through a 5% stop
        with db.tx() as c:
            c.execute("UPDATE daily_candles SET low=low*0.9 WHERE instrumentId='1' AND d=?", (self.s.days[i + 2],))
        stocks = swing.load_stocks(self.p)
        res = swing.backtest(stocks, {**self.p, "sl_mode": "pct", "sl_value": 5.0})
        t = res["trades"][0]
        self.assertEqual(t["reason"], "Stop loss")
        self.assertEqual(t["exit_day"], self.s.days[i + 2])
        self.assertAlmostEqual(t["exit"], t["entry"] * 0.95, delta=0.06)
        self.assertAlmostEqual(t["r"], -1.0, delta=0.1)

    def test_no_signal_below_sma(self):
        self.put("2", "BBB", series([300 - i * 0.5 for i in range(260)]))   # down-trend
        stocks = swing.load_stocks(self.p, symbols=["BBB"])
        self.assertEqual(swing.backtest(stocks, self.p)["trades"], [])

    def test_liquidity_filter(self):
        p = swing.params({"min_value_cr": 500})                  # ₹500 Cr/day: nothing qualifies
        self.assertEqual(swing.backtest(swing.load_stocks(p), p)["trades"], [])

    def test_max_positions_and_lowest_rsi_first(self):
        self.put("2", "BBB", series(uptrend_with_dip(step=0.035)))   # sharper dip -> lower RSI
        stocks = swing.load_stocks(self.p)
        res = swing.backtest(stocks, {**self.p, "max_positions": 1})
        self.assertEqual(res["trades"][0]["symbol"], "BBB")
        self.assertGreaterEqual(res["summary"]["skipped_full"], 1)
        both = swing.backtest(stocks, {**self.p, "max_positions": 2})
        self.assertEqual({t["symbol"] for t in both["trades"]}, {"AAA", "BBB"})

    def test_capital_is_conserved(self):
        res = swing.backtest(self.stocks, self.p)
        s = res["summary"]
        self.assertAlmostEqual(s["final_capital"] - s["initial_capital"],
                               sum(t["net"] for t in res["trades"]), delta=1)

    def test_scan_signal_on_latest_day(self):
        i = self.signal_day()
        cut = self.s.days[i]
        with db.tx() as c:
            c.execute("DELETE FROM daily_candles WHERE d>?", (cut,))
        r = swing.scan(swing.load_stocks(self.p), self.p)
        self.assertEqual(r["day"], cut)
        self.assertEqual([x["symbol"] for x in r["buy"]], ["AAA"])
        self.assertIn("history", r["buy"][0])
        self.assertGreater(r["buy"][0]["qty"], 0)


class TestPage(Base):
    def test_api(self):
        import time
        import app as webapp
        self.put("1", "AAA", series(uptrend_with_dip()))
        cl = webapp.app.test_client()
        with cl.session_transaction() as s:
            s["dashboard_authenticated"] = True
        r = cl.get("/iifl/api/swing/status")
        if r.status_code in (302, 401):
            self.skipTest("dashboard login wall")
        self.assertEqual(r.get_json()["result"]["with_data"], 1)
        job = cl.post("/iifl/api/swing/backtest", json={"params": {"min_value_cr": 0}}).get_json()["result"]["job"]
        for _ in range(50):
            j = cl.get(f"/iifl/api/swing/jobs/{job}").get_json()["result"]
            if j["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertEqual(j["result"]["summary"]["trades"], 1)
        self.assertEqual(cl.get("/iifl/swing").status_code, 200)


if __name__ == "__main__":
    unittest.main()
