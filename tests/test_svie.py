"""SVIE backtest + data tests. Run: python -m unittest -v tests.test_svie

Uses SYNTHETIC candles only to check the mechanics (no look-ahead, fills,
charges, limits). Synthetic results say nothing about real profitability.
"""

import os
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "IIFL_ENV_FILE": os.path.join(_TMP, "none.env"),
    "BOT_DB_FILE": os.path.join(_TMP, "s.db"),
    "BOT_DATA_FILE": os.path.join(_TMP, "bots.json"),
})

import svie_backtest as sb  # noqa: E402
import svie_data  # noqa: E402


def synth(days=25, seed=7, start=date(2026, 9, 14), px=62.0, drift=0.0):
    rnd = random.Random(seed)
    out, d = [], start
    while len({c["ts"][:10] for c in out}) < days:
        if d.weekday() < 5:
            for m in range(375):
                hh, mm = divmod(555 + m, 60)
                o = px
                c = max(1.0, o * (1 + rnd.gauss(drift, 0.0012)))
                h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0006)))
                l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0006)))
                vol = int(abs(rnd.gauss(12000, 6000)) * (3 if rnd.random() < 0.05 else 1))
                out.append({"ts": f"{d.isoformat()} {hh:02d}:{mm:02d}", "open": round(o, 2), "high": round(h, 2),
                            "low": round(l, 2), "close": round(c, 2), "volume": vol})
                px = c
        d += timedelta(days=1)
    return out


class TestIndicators(unittest.TestCase):
    def test_rsi_bounds_and_flat(self):
        r = sb.rsi_series([10 + (i % 3) for i in range(50)], 7)
        self.assertIsNone(r[6])
        self.assertTrue(all(0 <= x <= 100 for x in r[7:]))
        self.assertEqual(sb.rsi_series([float(i) for i in range(20)], 7)[-1], 100.0)

    def test_buy_volume_estimate(self):
        c = [{"ts": "2026-10-01 09:15", "open": 10, "high": 11, "low": 10, "close": 11, "volume": 100},
             {"ts": "2026-10-01 09:16", "open": 11, "high": 11, "low": 10, "close": 10, "volume": 100},
             {"ts": "2026-10-01 09:17", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100}]
        pr = sb.Prepared(c, sb.DEFAULTS)
        self.assertEqual(pr.buy_vol, [100.0, 0.0, 50.0])

    def test_vwap_resets_daily(self):
        c = synth(days=2)
        pr = sb.Prepared(c, sb.DEFAULTS)
        i = next(j for j in range(len(c)) if c[j]["ts"][:10] != c[0]["ts"][:10])
        k = c[i]
        self.assertAlmostEqual(pr.vwap[i], (k["high"] + k["low"] + k["close"]) / 3, places=6)

    def test_size_table(self):
        self.assertEqual(sb.size_fraction(85), 1.0)
        self.assertEqual(sb.size_fraction(70), 0.75)
        self.assertEqual(sb.size_fraction(55), 0.40)
        self.assertEqual(sb.size_fraction(40), 0.0)


class TestBacktest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = synth()
        cls.p = dict(sb.DEFAULTS)
        cls.pr, cls.a, cls.b, cls.res = sb.run_all(cls.c, cls.p, "2026-09-21", "2026-12-31")

    def test_all_three_run(self):
        self.assertEqual(set(self.res), {"A", "B", "C"})
        for r in self.res.values():
            self.assertEqual(len(r["equity"]), self.b - self.a)

    def test_buy_and_hold_holds_everything(self):
        m = self.res["A"]["metrics"]
        self.assertEqual(m["trades"], 0)
        self.assertGreater(m["positional_qty_end"], 0)
        self.assertGreater(m["max_exposure"], 0.9 * self.p["capital"] * 0.9)

    def test_participation_cap_and_next_bar_fills(self):
        for s in "ABC":
            sim, _ = sb.simulate(self.pr, s, self.p, self.a, self.b)
            for i, side, qty, px in sim.fill_log:
                k = self.c[i]
                if side == "BUY":
                    self.assertLessEqual(qty, int(k["volume"] * self.p["participation_pct"] / 100))
                    self.assertGreaterEqual(px, k["low"] - 0.01)
                # sells may be forced at the last bar of the day; otherwise capped
                if side == "SELL" and k["ts"][11:] != "15:29":
                    self.assertLessEqual(qty, int(k["volume"] * self.p["participation_pct"] / 100))
            self.assertGreaterEqual(sim.cash, 0)

    def test_intraday_flat_every_night(self):
        for s in "BC":
            for t in self.res[s]["trades"]:
                if t["sleeve"] == "INTRA":
                    self.assertEqual(t["entry_ts"][:10], t["exit_ts"][:10])
                    self.assertLessEqual(t["exit_ts"][11:], "15:29")

    def test_cash_reconciles(self):
        """final equity - capital == gross P&L - charges, for every strategy."""
        for s in "ABC":
            m = self.res[s]["metrics"]
            self.assertAlmostEqual(m["net_profit"], m["gross_profit"] - m["charges_total"], delta=1.0, msg=s)

    def test_charges_always_paid(self):
        for s in "BC":
            for t in self.res[s]["trades"]:
                self.assertGreater(t["charges"], 0)
                self.assertAlmostEqual(t["net"], t["gross"] - t["charges"], places=1)

    def test_capital_caps(self):
        sim, _ = sb.simulate(self.pr, "C", self.p, self.a, self.b)
        # rerun and watch sleeve costs bar by bar
        sim = sb.Sim(self.pr, "C", self.p, self.a, self.b)
        for i in range(self.a, self.b):
            sim.bar(i)
            intra = sum(l.open_qty * l.entry_px for l in sim.lots if l.sleeve == "INTRA")
            pos = sum(l.open_qty * l.entry_px for l in sim.lots if l.sleeve == "POS")
            self.assertLessEqual(intra, self.p["intraday_cap"] * 1.01)
            self.assertLessEqual(pos, self.p["positional_cap"] * 1.01)

    def test_no_look_ahead(self):
        """Changing candles AFTER a cut-off must not change anything before it."""
        cut = self.a + (self.b - self.a) // 2
        c2 = [dict(k) for k in self.c]
        rnd = random.Random(1)
        for k in c2[cut:]:
            f = 1 + rnd.uniform(-0.05, 0.05)
            k.update(open=k["open"] * f, high=k["high"] * f * 1.01, low=k["low"] * f * 0.99,
                     close=k["close"] * f, volume=k["volume"] * 3)
        pr2 = sb.Prepared(c2, self.p)
        for s in "ABC":
            s1, _ = sb.simulate(self.pr, s, self.p, self.a, self.b)
            s2, _ = sb.simulate(pr2, s, self.p, self.a, self.b)
            f1 = [f for f in s1.fill_log if f[0] < cut]
            f2 = [f for f in s2.fill_log if f[0] < cut]
            self.assertEqual(f1, f2, f"strategy {s} used future candles")

    def test_daily_loss_limit_stops_trading(self):
        p = dict(self.p, daily_loss_limit=1.0, min_score=0, gate_rvol=0, gate_buy_ratio=0)
        sim, _ = sb.simulate(self.pr, "C", p, self.a, self.b)
        self.assertTrue(any("DAILY LOSS" in e for e in sim.events))

    def test_kill_switch(self):
        p = dict(self.p, max_drawdown_kill=1.0)
        sim, m = sb.simulate(self.pr, "A", p, self.a, self.b)
        self.assertTrue(m["kill_switch"])

    def test_outputs_written(self):
        meta = {"symbol": "SYN", "from": "a", "to": "b", "candles": 1, "capital": 1, "slippage_pct": 0,
                "participation_pct": 0, "brokerage": 0}
        base = sb.write_outputs(self.res, _TMP, meta)
        for suf in ("_report.html", "_summary.json", "_C_trades.csv", "_A_equity.csv"):
            self.assertTrue(os.path.exists(base + suf))

    def test_sweep_runs(self):
        old = sb.SWEEP
        sb.SWEEP = {"B": {"step_pct": [0.5, 1.0]}, "C": {"min_score": [50, 80]}}
        try:
            r = sb.sweep(self.c, self.p, "2026-09-21", "2026-12-31")
        finally:
            sb.SWEEP = old
        self.assertEqual(r["results"]["C"]["combos"], 2)


class TestData(unittest.TestCase):
    def test_csv_roundtrip_and_quality(self):
        c = synth(days=3)
        path = os.path.join(_TMP, "x.csv")
        svie_data.write_csv(path, c)
        back = svie_data.read_csv(path)
        self.assertEqual(len(back), len(c))
        rep = svie_data.quality_report(back[:-10], date(2026, 9, 14), date(2026, 9, 16))
        self.assertEqual(rep["days_with_data"], 3)
        self.assertEqual(rep["missing_days"], [])
        self.assertEqual(len(rep["short_days"]), 1)

    def test_parse_quote_defensive(self):
        body = {"status": "Ok", "result": [{"ltp": "63.4", "totalBuyQuantity": 1000, "totalSellQuantity": 2500,
                                            "bids": [{"price": 63.35, "quantity": 400, "orders": 3}],
                                            "asks": [[63.4, 900, 5]]}]}
        s = svie_data.parse_quote(body)
        self.assertEqual(s["ltp"], 63.4)
        self.assertEqual((s["best_bid"], s["best_bid_qty"]), (63.35, 400))
        self.assertEqual((s["best_ask"], s["best_ask_qty"]), (63.4, 900))
        self.assertEqual(s["total_sell_qty"], 2500)
        self.assertIsNone(svie_data.parse_quote({"weird": 1})["ltp"])


if __name__ == "__main__":
    unittest.main()
