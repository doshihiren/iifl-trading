"""Swing auto-trader tests (fake broker). Run: python -m unittest -v tests.test_swing_live"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_engine import IST, Base  # noqa: E402  (sets up the temp DB + fake broker)

import db  # noqa: E402
import market_calendar as cal  # noqa: E402
import swing  # noqa: E402
import swing_live  # noqa: E402

IID = "500"


def trading_days_until(end, n):
    out, d = [], end
    while len(out) < n:
        if cal.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=IST)):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def dip_closes(n_up=230, dip=6):
    closes = [100 + i * 0.5 for i in range(n_up)]
    top = closes[-1]
    return closes + [top * (1 - 0.02 * (k + 1)) for k in range(dip)]


class SwingBase(Base):
    def setUp(self):
        super().setUp()
        swing.clear_cache()
        closes = dip_closes()
        ds = trading_days_until(date(2026, 10, 5), len(closes))       # last candle Mon 5 Oct (2 Oct holiday)
        rows, prev = [], closes[0]
        for d, c in zip(ds, closes):
            rows.append((IID, d, prev, max(prev, c) * 1.005, min(prev, c) * 0.995, c, 1_000_000))
            prev = c
        with db.tx() as c:
            c.execute("INSERT INTO swing_universe(instrumentId, symbol, tick_size) VALUES(?, 'AAA', 0.05)", (IID,))
            c.executemany("INSERT INTO daily_candles VALUES(?,?,?,?,?,?,?)", rows)
        self.last_close = closes[-1]
        self.prices[IID] = round(self.last_close, 2)
        swing._put_state(state="DONE", history_done=True)
        swing_live.save_cfg(status="RUNNING", mode="LIVE", capital=100000, max_positions=5,
                            params={"min_value_cr": 0, "min_price": 0})

    def evening(self, d=date(2026, 10, 5)):
        return swing_live.make_plan(datetime(d.year, d.month, d.day, 18, 0, tzinfo=IST))

    def morning(self, d=date(2026, 10, 6), hhmm=(9, 15, 30)):
        self.now = datetime(d.year, d.month, d.day, *hhmm, tzinfo=IST)

    def add_candle(self, d, close):
        with db.tx() as c:
            c.execute("INSERT INTO daily_candles VALUES(?,?,?,?,?,?,?)",
                      (IID, d, close, close * 1.01, close * 0.99, close, 1_000_000))

    def buys(self):
        return [p for p in self.broker.placed if p[0]["transactionType"] == "BUY"]

    def sells(self):
        return [p for p in self.broker.placed if p[0]["transactionType"] == "SELL"]

    def bought_lot(self):
        self.evening()
        self.morning()
        self.tick()
        self.broker.fill(db.row("SELECT broker_order_id FROM orders WHERE side='BUY'")["broker_order_id"], self.prices[IID])
        self.tick()
        return db.row("SELECT * FROM trades WHERE bot_id='SW-AAA'")


class TestSwingLive(SwingBase):
    def test_plan_buy_once_and_actual_fill(self):
        plan = self.evening()
        self.assertEqual(plan["day"], "2026-10-06")
        self.assertEqual([e["symbol"] for e in plan["entries"]], ["AAA"])
        self.morning(hhmm=(9, 10, 0))
        self.tick()
        self.assertEqual(self.broker.placed, [])                  # market not open yet
        self.morning()
        self.tick(); self.tick()
        self.assertEqual(len(self.buys()), 1)
        p = self.buys()[0][0]
        self.assertEqual(p["product"], "DELIVERY")
        self.assertEqual(int(p["quantity"]), int(20000 // self.prices[IID]))
        self.restart()
        self.tick()
        self.assertEqual(len(self.buys()), 1)                     # restart never buys twice
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM trades"), 0)   # accepted is not a trade
        self.broker.fill(db.row("SELECT broker_order_id FROM orders")["broker_order_id"], 106.1)
        self.tick()
        lot = db.row("SELECT * FROM trades")
        self.assertEqual((lot["bot_id"], lot["entry_price"], lot["status"]), ("SW-AAA", 106.1, "OPEN"))
        self.assertEqual(swing_live.get_plan()["entries"][0]["status"], "ORDERED")

    def test_rsi_exit_sells_next_open(self):
        lot = self.bought_lot()
        self.prices[IID] = round(self.last_close * 1.15, 2)
        self.tick()
        self.assertEqual(self.sells(), [])                        # no target-style selling intraday
        self.add_candle("2026-10-06", self.last_close * 1.15)                      # strong close -> RSI above 40
        plan = self.evening(date(2026, 10, 6))
        self.assertEqual([x["symbol"] for x in plan["exits"]], ["AAA"])
        self.assertIn("RSI", db.row("SELECT exit_signal FROM trades WHERE id=?", (lot["id"],))["exit_signal"])
        self.morning(date(2026, 10, 7))
        self.tick()
        self.assertEqual(len(self.sells()), 1)
        self.broker.fill(db.row("SELECT broker_order_id FROM orders WHERE side='SELL'")["broker_order_id"], 216.0)
        self.tick()
        t = db.row("SELECT * FROM trades WHERE id=?", (lot["id"],))
        self.assertEqual((t["status"], t["exit_price"]), ("CLOSED", 216.0))
        self.assertIn("RSI", t["exit_reason"])

    def test_time_exit_after_max_days(self):
        swing_live.save_cfg(params={"min_value_cr": 0, "min_price": 0, "max_hold": 2, "exit_rsi": 99})
        self.bought_lot()
        self.add_candle("2026-10-06", self.last_close)
        self.evening(date(2026, 10, 6))
        self.assertIsNone(db.row("SELECT exit_signal FROM trades")["exit_signal"])
        self.add_candle("2026-10-07", self.last_close)
        self.evening(date(2026, 10, 7))
        self.assertIn("2 days held", db.row("SELECT exit_signal FROM trades")["exit_signal"])

    def test_stop_keeps_exits_but_no_new_buys(self):
        self.bought_lot()
        swing_live.save_cfg(status="STOPPED")
        self.add_candle("2026-10-06", self.last_close * 1.15)
        plan = self.evening(date(2026, 10, 6))
        self.assertEqual(plan["entries"], [])
        self.assertEqual(len(plan["exits"]), 1)
        self.morning(date(2026, 10, 7))
        self.tick()
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(len(self.buys()), 1)

    def test_deadline_skips_late_buys(self):
        self.evening()
        self.morning(hhmm=(9, 50, 0))
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(swing_live.get_plan()["entries"][0]["status"], "SKIPPED")

    def test_session_expired_waits_then_buys(self):
        self.evening()
        self.morning()
        self.broker.session_valid = False
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.broker.session_valid = True
        self.engine.last_session_check = None
        self.tick()
        self.assertEqual(len(self.buys()), 1)

    def test_stop_loss(self):
        swing_live.save_cfg(params={"min_value_cr": 0, "min_price": 0, "sl_mode": "pct", "sl_value": 5})
        lot = self.bought_lot()
        self.morning(date(2026, 10, 7))
        self.prices[IID] = round(lot["entry_price"] * 0.94, 2)
        self.tick()
        self.assertEqual(len(self.sells()), 1)
        self.assertIn("Stop loss", db.row("SELECT exit_reason FROM trades")["exit_reason"])

    def test_slot_limit(self):
        swing_live.save_cfg(max_positions=1)
        self.bought_lot()
        plan = self.evening(date(2026, 10, 5))                    # same data day: no rebuild
        self.assertIsNone(plan)
        self.add_candle("2026-10-06", self.last_close * 0.98)                       # still oversold, still held
        plan = self.evening(date(2026, 10, 6))
        self.assertEqual(plan["entries"], [])                     # already holding AAA

    def test_budget_too_small(self):
        swing_live.save_cfg(capital=500, max_positions=5)         # ₹100 per stock < price
        self.evening()
        self.morning()
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(swing_live.get_plan()["entries"][0]["status"], "SKIPPED")

    def test_paper_mode(self):
        swing_live.save_cfg(mode="PAPER")
        self.evening()
        self.morning()
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(db.row("SELECT mode, status FROM trades")["mode"], "PAPER")


if __name__ == "__main__":
    unittest.main()
