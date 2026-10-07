"""Strategy Lab engine tests. Run: python -m unittest -v tests.test_lab"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import lab_backtest as lb  # noqa: E402

G = {"core_capital": 500000, "active_capital": 500000, "tick": 0.01, "brokerage": 0, "charges_pct": 0,
     "daily_loss_limit": 0}


def mk(day, hhmm, o, h=None, l=None, c=None, v=1000):
    h = max(o, c if c is not None else o) if h is None else h
    l = min(o, c if c is not None else o) if l is None else l
    c = o if c is None else c
    return {"ts": f"{day} {hhmm}", "open": o, "high": h, "low": l, "close": c, "volume": v}


def minutes(day, start, n, price=100.0, v=1000):
    out = []
    for k in range(n):
        t = start + k
        out.append(mk(day, f"{t // 60:02d}:{t % 60:02d}", price, v=v))
    return out


def run(candles, strat, p, g=None, tf="1m", **prep_kw):
    prep = lb.Prepared(candles, tf, **prep_kw)
    return lb.simulate(prep, strat, p, {**G, **(g or {})})


class TestCharges(unittest.TestCase):
    def test_delivery_buy(self):
        ch = lb.leg_charges(100000, "BUY", "DELIVERY", 20)
        # STT 100 + stamp 15 + exch 2.97 + SEBI 0.10 + GST 18% of (20 + 2.97 + 0.10) + brokerage 20
        self.assertAlmostEqual(ch, 100 + 15 + 2.97 + 0.10 + 0.18 * 23.07 + 20, places=2)

    def test_intraday_sell(self):
        ch = lb.leg_charges(100000, "SELL", "INTRADAY", 0)
        self.assertAlmostEqual(ch, 25 + 2.97 + 0.10 + 0.18 * 3.07, places=2)

    def test_custom_pct(self):
        self.assertAlmostEqual(lb.leg_charges(50000, "BUY", "DELIVERY", 20, custom_pct=0.1), 50.0)


class TestIndicators(unittest.TestCase):
    def test_bars_vwap_orb(self):
        cs = [mk("2026-09-01", "09:15", 100, 102, 99, 101, v=100), mk("2026-09-01", "09:16", 101, 104, 100, 103, v=300)]
        cs += minutes("2026-09-01", 9 * 60 + 17, 13, 103)
        prep = lb.Prepared(cs, "5m", orb_minutes=15)
        self.assertEqual(len(prep.bars), 3)
        tp1, tp2 = (102 + 99 + 101) / 3, (104 + 100 + 103) / 3
        first = (tp1 * 100 + tp2 * 300 + 103 * 3000) / 3400
        self.assertAlmostEqual(prep.vwap[0], first, places=6)
        self.assertEqual(prep.orb["2026-09-01"], (104, 99))

    def test_rsi_rising_is_100(self):
        cs = [mk("2026-09-01", f"09:{15 + k:02d}", 100 + k) for k in range(20)]
        prep = lb.Prepared(cs, "1m", rsi_period=14)
        self.assertIsNone(prep.rsi[13])
        self.assertEqual(prep.rsi[14], 100.0)


class TestScalp(unittest.TestCase):
    P = {"target_pct": 1.0, "sl_pct": 1.0, "max_positions": 1, "shares": 10, "sizing": "shares"}

    def test_signal_fills_next_open_then_target(self):
        cs = [mk("2026-09-01", "09:15", 100), mk("2026-09-01", "09:16", 100, h=101.5), mk("2026-09-01", "09:17", 100)]
        r = run(cs, "D", self.P)
        t = r["trades"][-1]
        self.assertEqual((t["entry_ts"][11:], t["entry"], t["exit"], t["reason"]), ("09:16", 100, 101.0, "Target"))
        self.assertEqual(t["net"], 10.0)

    def test_both_levels_in_one_candle_assumes_stop(self):
        cs = [mk("2026-09-01", "09:15", 100), mk("2026-09-01", "09:16", 100, h=102, l=98)]
        t = run(cs, "D", self.P)["trades"][-1]
        self.assertEqual((t["exit"], t["reason"]), (99.0, "Stop"))

    def test_gap_down_fills_at_open(self):
        cs = [mk("2026-09-01", "09:15", 100), mk("2026-09-01", "09:16", 100), mk("2026-09-01", "09:17", 97)]
        t = run(cs, "D", self.P)["trades"][-1]
        self.assertEqual((t["exit"], t["reason"]), (97, "Stop (gap)"))

    def test_square_off_and_last_entry(self):
        cs = minutes("2026-09-01", 14 * 60 + 58, 15, 100)      # 14:58 .. 15:12
        r = run(cs, "D", self.P)
        self.assertEqual([t["reason"] for t in r["trades"]], ["Square-off 15:10"])
        self.assertEqual(r["trades"][0]["entry_ts"][11:], "14:59")   # 15:00 bar close is too late
        self.assertEqual(r["summary"]["open_count"], 0)

    def test_risk_sizing(self):
        cs = [mk("2026-09-01", "09:15", 100), mk("2026-09-01", "09:16", 100)]
        r = run(cs, "D", {**self.P, "sizing": "risk", "risk_per_trade": 2500})
        self.assertEqual(r["open"][0]["qty"], 2500)     # risk 1.00/share -> 2500 shares


class TestBreakout(unittest.TestCase):
    def test_orb_volume_breakout(self):
        d = "2026-09-01"
        cs = minutes(d, 9 * 60 + 15, 15, 100, v=1000)                 # range 100/100
        cs[3] = mk(d, "09:18", 100, 101, 99, 100, v=1000)             # range 101/99
        cs += minutes(d, 9 * 60 + 30, 20, 100.5, v=1000)              # inside range
        cs.append(mk(d, "09:50", 100.5, 102, 100.5, 101.8, v=5000))   # close > 101 on 5x volume
        cs.append(mk(d, "09:51", 102, 102, 101.9, 102))               # fill at 102
        cs.append(mk(d, "09:52", 102, 108, 102, 107))                 # target = 102 + 2 x (102 - 99) = 108
        p = {"vol_mult": 2, "r_multiple": 2, "sl_mode": "orb", "sl_pct": 1, "shares": 10, "sizing": "shares",
             "max_trades_day": 1, "max_positions": 1}
        t = run(cs, "C", p)["trades"][-1]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (102, 108, "Target"))

    def test_no_breakout_without_volume(self):
        d = "2026-09-01"
        cs = minutes(d, 9 * 60 + 15, 40, 100, v=1000)
        cs.append(mk(d, "09:55", 100, 103, 100, 102.5, v=1000))
        cs.append(mk(d, "09:56", 102.5))
        p = {"vol_mult": 2, "r_multiple": 2, "sl_mode": "orb", "sl_pct": 1, "shares": 10, "max_trades_day": 1}
        self.assertEqual(run(cs, "C", p)["summary"]["trades"], 0)


class TestLadder(unittest.TestCase):
    def test_spacing_target_and_core(self):
        d = "2026-09-01"
        prices = [100, 100, 99.5, 98.9, 98.0, 99.1]
        cs = [mk(d, f"09:{15 + k:02d}", px) for k, px in enumerate(prices)]
        p = {"spacing_pct": 1.0, "target_pct": 1.0, "rsi_max": None, "shares": 10, "sizing": "shares",
             "max_positions": 10, "core_qty": 5}
        r = run(cs, "A", p)
        buys = sorted(t["entry"] for t in r["trades"]) + sorted(x["entry"] for x in r["open"])
        # 09:15 signal -> buy 100 @09:16; next buy needs close <= 99.00 -> 98.9 close @09:18 -> buy 98.0 @09:19
        # lot @98.0 target 98.98 -> sold @09:20 (open 99.1 gaps above)
        self.assertEqual(buys, [98.0, 100])
        self.assertEqual(r["trades"][0]["reason"], "Target (gap)")
        self.assertEqual(r["summary"]["core_qty"], 5)
        self.assertEqual(r["summary"]["core_pnl"], round((99.1 - 100) * 5, 2))


class TestDailyLimit(unittest.TestCase):
    def test_limit_blocks_rest_of_day(self):
        d = "2026-09-01"
        cs = [mk(d, "09:15", 100), mk(d, "09:16", 100), mk(d, "09:17", 90)]   # -10/share x 100 sh = -1000
        cs += minutes(d, 9 * 60 + 18, 10, 90)
        cs += minutes("2026-09-02", 9 * 60 + 15, 3, 90)
        p = {"target_pct": 5, "sl_pct": 5, "max_positions": 1, "shares": 100, "sizing": "shares"}
        r = run(cs, "D", p, g={"daily_loss_limit": 500})
        day1 = [t for t in r["trades"] if t["entry_ts"].startswith(d)]
        self.assertEqual(len(day1), 1)
        self.assertEqual(r["summary"]["limit_days"], 1)
        self.assertTrue(any(t["entry_ts"].startswith("2026-09-02") for t in r["trades"] + r["open"]))


class TestOptimizer(unittest.TestCase):
    def test_runs_grid_and_tests_best(self):
        d1, d2 = "2026-09-01", "2026-09-02"
        cs = []
        for d in (d1, d2):
            for k in range(60):
                t = 9 * 60 + 15 + k
                px = 100 + (k % 6) * 0.3
                cs.append(mk(d, f"{t // 60:02d}:{t % 60:02d}", px, h=px + 0.4, l=px - 0.4))
        prep = lb.Prepared(cs, "1m")
        tr, te = lb.index_range(prep, d1, d1), lb.index_range(prep, d2, d2)
        out = lb.optimize(prep, "D", {"shares": 10, "sizing": "shares"}, G, tr, te, top=3)
        self.assertEqual(out["tested"], 27)
        self.assertEqual(len(out["best"]), 3)
        self.assertIsNotNone(out["best"][0]["test"])


if __name__ == "__main__":
    unittest.main()
