"""Camarilla + RSI tests. Run: python -m unittest -v tests.test_camarilla"""

import os
import random
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import camarilla_strategy as cs  # noqa: E402


def minute_ts(day, k):
    m = 9 * 60 + 15 + k
    return f"{day} {m // 60:02d}:{m % 60:02d}"


def synth(days=20, seed=3, start=26000.0):
    rnd = random.Random(seed)
    out, p, d, n = [], start, date(2026, 9, 1), 0
    while n < days:
        if d.weekday() < 5:
            for k in range(375):
                o = p
                c = o * (1 + rnd.gauss(0, 0.0006))
                out.append({"ts": minute_ts(d.isoformat(), k), "open": o, "high": max(o, c) * 1.0002,
                            "low": min(o, c) * 0.9998, "close": c, "volume": 0})
                p = c
            n += 1
        d += timedelta(days=1)
    return out


class CamarillaTests(unittest.TestCase):
    def test_levels_match_formula(self):
        lv = cs.camarilla(110, 100, 105)
        self.assertAlmostEqual(lv["R4"], 105 + 11 / 2)
        self.assertAlmostEqual(lv["S2"], 105 - 11 / 6)
        self.assertTrue(lv["S4"] < lv["S2"] < lv["R2"] < lv["R4"])

    def test_resample_15m(self):
        data = synth(days=1)
        bars = cs.resample(data, 15)
        self.assertEqual(len(bars), 25)
        self.assertEqual(bars[0]["ts"][11:], "09:15")
        self.assertEqual(bars[-1]["ts"][11:], "15:15")
        self.assertEqual(bars[0]["high"], max(c["high"] for c in data[:15]))

    def test_rsi_bounds(self):
        r = cs.rsi_wilder([100 + i for i in range(40)], 21)
        self.assertIsNone(r[20])
        self.assertEqual(r[-1], 100.0)

    def test_signals_follow_rules(self):
        data = synth()
        bars, rsi, levels, sigs = cs.signals(data)
        self.assertTrue(sigs)
        for s in sigs:
            if not s["taken"]:
                continue
            lv = levels[s["ts"][:10]]
            if s["side"] == "BUY":
                self.assertIn(s["level"], ("S2", "S4"))
                self.assertLess(s["sl"], s["entry"])
                self.assertGreater(s["target"], s["entry"])
                self.assertAlmostEqual(s["target"], round(lv["S2" if s["level"] == "S4" else "R2"], 2))
            else:
                self.assertIn(s["level"], ("R2", "R4"))
                self.assertGreater(s["sl"], s["entry"])
                self.assertLess(s["target"], s["entry"])

    def test_first_day_has_no_pivots(self):
        data = synth(days=3)
        _, _, levels, _ = cs.signals(data)
        self.assertNotIn(data[0]["ts"][:10], levels)

    def test_backtest_one_trade_at_a_time_and_intraday(self):
        out = cs.backtest(synth(days=30))
        trades = out["trades"]
        self.assertEqual(out["summary"]["trades"], len(trades))
        for t in trades:
            self.assertLessEqual(t["exit_time"], "15:15")
            self.assertGreaterEqual(t["exit_time"], t["entry_time"])
        for a, b in zip(trades, trades[1:]):
            if a["date"] == b["date"]:
                self.assertGreaterEqual(b["entry_time"], a["exit_time"])

    def test_current_state(self):
        st = cs.current_state(synth(days=5))
        self.assertIn("rsi", st)
        self.assertEqual(set(st["levels"]), {"S4", "S2", "R2", "R4"})

    def test_min_rr_filters_close_targets(self):
        data = synth(days=30)
        off = cs.backtest(data, {"min_rr": 0})
        on = cs.backtest(data, {"min_rr": 1.5})
        self.assertLessEqual(on["summary"]["trades"], off["summary"]["trades"])
        for s in on["signals"]:
            if s["taken"]:
                self.assertGreaterEqual(abs(s["target"] - s["entry"]) / abs(s["entry"] - s["sl"]), 1.5 - 1e-6)

    def test_gap_guard_skips_day_after_missing_day(self):
        data = synth(days=6)
        days = sorted({c["ts"][:10] for c in data})
        holey = [c for c in data if c["ts"][:10] != days[2]]
        weekday = lambda d: d.weekday() < 5
        levels, _, missing = cs.daily_levels(holey, is_trading_day=weekday)
        self.assertIn(days[2], missing)
        self.assertNotIn(days[3], levels)          # its real previous day is missing
        self.assertIn(days[4], levels)
        levels2, _, _ = cs.daily_levels(holey)     # without the guard it uses the older day
        self.assertIn(days[3], levels2)

    def test_special_session_on_holiday_is_ignored(self):
        data = synth(days=4)
        days = sorted({c["ts"][:10] for c in data})
        holiday = days[1]
        is_td = lambda d: d.weekday() < 5 and d.isoformat() != holiday
        levels, _, missing = cs.daily_levels(data, is_trading_day=is_td)
        self.assertNotIn(holiday, levels)          # no trading on the special session
        self.assertIn(days[2], levels)             # next day uses the last full day, not the special session
        self.assertEqual(missing, [])
        first = [c for c in data if c["ts"][:10] == days[0]]
        self.assertAlmostEqual(levels[days[2]]["R4"], cs.camarilla(max(c["high"] for c in first),
                               min(c["low"] for c in first), first[-1]["close"])["R4"])

    def test_optimize_tunes_and_checks_separately(self):
        data = synth(days=30)
        days = sorted({c["ts"][:10] for c in data})
        small = {"bands": [(40, 60)], "min_rr": [0, 1], "max_sl_pct": [0.25], "level_tol_pct": [0.05],
                 "touch_lookback": [2]}
        o = cs.optimize(data, {}, (days[1], days[19]), (days[20], days[-1]), grid=small)
        self.assertEqual(o["tested"], 2)
        self.assertIn("check", o["current"])
        for b in o["best"]:
            self.assertIn(b["settings"]["min_rr"], (0, 1))

    def test_trend_filter_blocks_trades_against_trend(self):
        data = synth(days=40)
        for mode in ("daily", "ema"):
            bars, _, _, sigs = cs.signals(data, {"trend_filter": mode})
            for s in sigs:
                if s["taken"]:
                    self.assertEqual(s["trend"], "up" if s["side"] == "BUY" else "down")
            self.assertTrue(any(s["reason"] == "against the trend" for s in sigs))

    def test_mid_target_is_halfway(self):
        data = synth(days=30)
        _, _, levels, sigs = cs.signals(data, {"target_mode": "mid"})
        for s in sigs:
            if s["taken"]:
                lv = levels[s["ts"][:10]]
                nxt = (cs.NEXT_UP if s["side"] == "BUY" else cs.NEXT_DOWN)[s["level"]]
                self.assertAlmostEqual(s["target"], round((lv[s["level"]] + lv[nxt]) / 2, 2), places=2)

    def test_daily_trend(self):
        days = {f"2026-09-{d:02d}": {"close": c} for d, c in zip(range(1, 8), [10, 11, 12, 13, 14, 15, 9])}
        t = cs.daily_trend(days)
        self.assertEqual(t["2026-09-06"], "up")      # 14 > avg(10..14)
        self.assertNotIn("2026-09-05", t)            # not enough history


if __name__ == "__main__":
    unittest.main()
