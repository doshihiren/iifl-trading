"""RSI Analysis tests. Run: python -m unittest -v tests.test_rsi_analysis"""

import logging
import os
import random
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_TMP = tempfile.mkdtemp()
os.environ.update({
    "IIFL_ENV_FILE": os.path.join(_TMP, "none.env"),
    "BOT_DB_FILE": os.path.join(_TMP, "r.db"),
    "BOT_DATA_FILE": os.path.join(_TMP, "bots.json"),
})

import backtest  # noqa: E402
import rsi_backtest as rb  # noqa: E402

logging.disable(logging.CRITICAL)


def bar(ts, o, h=None, l=None, c=None):
    return {"ts": ts, "open": o, "high": o if h is None else h, "low": o if l is None else l,
            "close": o if c is None else c, "volume": 0}


def minute_ts(day, k):
    m = 9 * 60 + 15 + k
    return f"{day} {m // 60:02d}:{m % 60:02d}"


def synth(days=15, seed=1, start=1000.0, vol=0.0009):
    rnd = random.Random(seed)
    out, p, d, n = [], start, date(2026, 6, 1), 0
    while n < days:
        if d.weekday() < 5:
            p *= 1 + rnd.gauss(0, 0.006)
            for k in range(375):
                o = p
                c = o * (1 + rnd.gauss(0, vol))
                h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
                l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
                out.append(bar(minute_ts(d, k), round(o, 2), round(h, 2), round(l, 2), round(c, 2)))
                p = c
            n += 1
        d += timedelta(days=1)
    return out


BASE = {"target_pct": 1.0, "shares": 10, "max_open": 30, "capital_limit": None, "tick": 0.05, "charges_pct": 0.1}


class TestRsi(unittest.TestCase):
    def reference(self, closes, n):
        # Independent Wilder RSI: SMA seed then alpha = 1/n smoothing.
        gains = [max(0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        losses = [max(0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
        ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
        vals = {n: 100 - 100 / (1 + ag / al)}
        for i in range(n, len(gains)):
            ag += (gains[i] - ag) / n
            al += (losses[i] - al) / n
            vals[i + 1] = 100 - 100 / (1 + ag / al)
        return vals

    def test_matches_reference(self):
        rnd = random.Random(3)
        closes = [100.0]
        for _ in range(400):
            closes.append(closes[-1] * (1 + rnd.gauss(0, 0.003)))
        got = rb.rsi_series(closes, 14)
        ref = self.reference(closes, 14)
        self.assertIsNone(got[13])
        for i, v in ref.items():
            self.assertAlmostEqual(got[i], v, places=9)

    def test_extremes(self):
        self.assertEqual(rb.rsi_series([float(i) for i in range(30)], 14)[-1], 100.0)
        self.assertEqual(rb.rsi_series([10.0] * 30, 14)[-1], 50.0)
        self.assertLess(rb.rsi_series([float(30 - i) for i in range(30)], 14)[-1], 1)


class TestParity(unittest.TestCase):
    def test_rsi_off_equals_classic_backtest(self):
        for seed in (1, 2, 3):
            cs = synth(12, seed)
            for tf in rb.TIMEFRAMES:
                for extra in ({}, {"batch_size": 4, "rebuy_dip_pct": 0.5}, {"capital_limit": 150000}):
                    p = dict(BASE, **extra)
                    a = backtest.simulate(cs, p, tf, detail=False)["summary"]
                    b = rb.simulate(rb.Prepared(cs, warmup=0), p, tf, detail=False)["summary"]
                    for k in ("entries", "closed", "net_pnl", "open_count", "unrealized", "peak_capital",
                              "max_drawdown", "skipped_max_open", "skipped_capital", "skipped_batch_wait"):
                        self.assertAlmostEqual(a[k] or 0, b[k] or 0, places=6, msg=(seed, tf, extra, k))


class TestNoLookahead(unittest.TestCase):
    def test_entry_decision_ignores_current_candle(self):
        cs = synth(4, 5)
        prep = rb.Prepared(cs)
        i = next(j for j in range(prep.start, prep.n) if prep.minute[j] % 15 == 0)
        p = dict(BASE, max_entry_rsi=55)
        a = rb.simulate(prep, p, "15m", prep.start, i + 1, detail=False)["summary"]
        cs2 = [dict(k) for k in cs]
        cs2[i]["close"] = cs2[i]["open"] * 1.2          # wild close on the decision candle itself
        cs2[i]["high"] = cs2[i]["close"]
        b = rb.simulate(rb.Prepared(cs2), p, "15m", prep.start, i + 1, detail=False)["summary"]
        self.assertEqual(a["entries"] + a["skipped_rsi_high"], b["entries"] + b["skipped_rsi_high"])
        self.assertEqual(a["skipped_rsi_high"], b["skipped_rsi_high"])


def ramp(n=60, start=100.0, step=0.5, day="2026-06-01"):
    """Steadily rising prices: RSI 100 after warm-up."""
    return [bar(minute_ts(day, k), start + k * step, start + k * step, start + k * step, start + k * step) for k in range(n)]


class TestRules(unittest.TestCase):
    def test_high_rsi_blocks_entries(self):
        cs = ramp(80)
        prep = rb.Prepared(cs, warmup=20)
        s = rb.simulate(prep, dict(BASE, max_entry_rsi=70), "1m", detail=False)["summary"]
        self.assertEqual(s["entries"], 0)
        self.assertGreater(s["skipped_rsi_high"], 0)
        s2 = rb.simulate(prep, dict(BASE), "1m", detail=False)["summary"]
        self.assertGreater(s2["entries"], 0)

    def test_falling_knife_filter(self):
        cs = [bar(minute_ts("2026-06-01", k), 200 - k, 200 - k, 200 - k, 200 - k) for k in range(80)]
        prep = rb.Prepared(cs, warmup=20)
        s = rb.simulate(prep, dict(BASE, min_entry_rsi=25), "1m", detail=False)["summary"]
        self.assertEqual(s["entries"], 0)
        self.assertGreater(s["skipped_rsi_low"], 0)

    def test_ride_past_target_then_book_at_rsi_peak(self):
        # Rising prices: target reached with RSI high -> ride; exit at RSI >= 80 next open.
        cs = ramp(40, 100, 0.5)
        prep = rb.Prepared(cs, warmup=20)
        p = dict(BASE, target_pct=1.0, max_open=1, hold_rsi=60, exit_rsi=80, max_stretch=0)
        out = rb.simulate(prep, p, "15m", detail=True)
        s = out["summary"]
        self.assertGreaterEqual(s["rides"], 1)
        closed = [t for t in out["trades"] if t["status"] == "Closed"]
        self.assertTrue(closed)
        t = closed[-1]
        self.assertEqual(t["reason"], rb.REASONS["RSI_PEAK"])
        self.assertGreater(t["exit"], t["target"])          # booked above the plain target
        self.assertGreater(t["extra"], 0)

    def test_riding_lot_never_below_floor_intrabar(self):
        cs = ramp(30, 100, 0.5)
        last = cs[-1]["close"]
        # price pops above target then collapses inside one candle
        cs.append(bar(minute_ts("2026-06-01", 30), last, last * 1.05, last * 0.9, last * 0.9))
        prep = rb.Prepared(cs, warmup=20)
        p = dict(BASE, max_open=1, hold_rsi=60, exit_rsi=101, max_stretch=0)
        out = rb.simulate(prep, p, "15m", detail=True)
        for t in out["trades"]:
            if t["status"] == "Closed":
                self.assertGreaterEqual(t["exit"], t["target"] - 1e-9)

    def test_plain_target_when_rsi_weak(self):
        cs = synth(5, 9)
        prep = rb.Prepared(cs)
        s = rb.simulate(prep, dict(BASE, hold_rsi=99.9, exit_rsi=100), "15m", detail=False)["summary"]
        r = s["exits_by_reason"]
        self.assertEqual(r["RSI_PEAK"] + r["RSI_FADE"] + r["FLOOR"] + r["CAP"], 0)


class TestHoldings(unittest.TestCase):
    def test_parse(self):
        rows, errs = rb.parse_holdings("100 @ 2450.5\n50 2400\n20,₹1999\n\n# note\nbad")
        self.assertEqual(rows, [(100, 2450.5), (50, 2400.0), (20, 1999.0)])
        self.assertEqual(len(errs), 1)

    def test_ladder_sells_only_above_min_price(self):
        cs = ramp(60, 100, 0.5)
        prep = rb.Prepared(cs, warmup=20)
        hold = {"rows": [(90, 100.0), (30, 200.0)], "book_rsi": 70, "parts": 3, "step": 5, "min_profit_pct": 1}
        out = rb.simulate(prep, dict(BASE, max_open=0), "15m", detail=False, holdings=hold)
        h = out["holdings"]
        sold = [r for r in h["rows"] if r["sold_ts"]]
        self.assertEqual(sum(r["qty"] for r in h["rows"] if r["buy"] == 100.0), 90)
        self.assertTrue(all(r["sold_px"] >= r["min_px"] for r in sold))
        self.assertTrue(all(r["buy"] == 100.0 for r in sold))   # 200-buy lots never reach min price
        self.assertEqual(h["remaining"], 30)

    def test_current_signal(self):
        cs = ramp(60, 100, 0.5)
        sig = rb.current_signal(cs, {"max_entry_rsi": 70},
                                {"rows": [(10, 100.0)], "book_rsi": 70, "parts": 1, "min_profit_pct": 1})
        self.assertEqual(sig["entry"]["state"], "BLOCKED")
        self.assertEqual(sig["holdings"][0]["action"], "SELL NOW")


class TestOptimizer(unittest.TestCase):
    def test_walk_forward(self):
        cs = synth(12, 4)
        prep = rb.Prepared(cs)
        base = dict(BASE, max_stretch=3, timeframe="15m")
        t = time.time()
        res = rb.optimize(prep, base)
        self.assertLess(time.time() - t, 120)
        self.assertTrue(res["split"]["enabled"])
        self.assertLess(res["split"]["train_to"], res["split"]["test_from"])
        keys = {(r["settings"]["timeframe"], r["settings"]["target_pct"], r["settings"]["max_entry_rsi"]) for r in res["top"]}
        self.assertEqual(len(keys), len(res["top"]))          # all suggestions differ
        for r in res["top"]:
            self.assertIn(r["verdict"], ("RELIABLE", "OK", "PAST_ONLY"))
        self.assertIsNone(res["baseline"]["settings"]["max_entry_rsi"])

    def test_holdings_optimizer(self):
        cs = synth(6, 2)
        prep = rb.Prepared(cs)
        hold = {"rows": [(30, cs[150]["close"])], "book_rsi": 70, "parts": 1, "min_profit_pct": 0.5}
        res = rb.optimize_holdings(prep, hold, dict(BASE))
        self.assertEqual(len(res), len(rb.HOLD_GRID["book_rsi"]) * len(rb.HOLD_GRID["parts"]))
        self.assertGreaterEqual(res[0]["end_value"], res[-1]["end_value"])


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import db
        import app as app_mod
        db.init_db()
        cls.db = db
        cls.client = app_mod.app.test_client()
        with cls.client.session_transaction() as s:
            s["dashboard_authenticated"] = True
        cs = synth(12, 6)
        with db.tx() as c:
            c.execute("INSERT OR REPLACE INTO analysis_symbols(instrumentId, symbol, tradingSymbol, exchange, tick_size, live, "
                      "sync_requested, created_at, updated_at) VALUES('999','TEST','TEST-EQ','NSEEQ',0.05,0,0,'x','x')")
            c.executemany("INSERT OR REPLACE INTO candles(instrumentId, ts, open, high, low, close, volume) VALUES('999',?,?,?,?,?,0)",
                          [(k["ts"], k["open"], k["high"], k["low"], k["close"]) for k in cs])
        cls.cs = cs

    def body(self, **kw):
        b = {"instrumentId": "999", "timeframe": "15m", "from": self.cs[0]["ts"][:10], "to": self.cs[-1]["ts"][:10],
             "target_pct": 1, "shares": 10, "max_open": 20, "charges_pct": 0.1, "max_entry_rsi": 70, "hold_rsi": 60,
             "exit_rsi": 78, "max_stretch": 3, "holdings": "50 @ " + str(self.cs[400]["close"]), "book_rsi": 70,
             "book_parts": 2, "book_step": 5, "min_profit_pct": 0.5}
        b.update(kw)
        return b

    def test_page(self):
        r = self.client.get("/iifl/analysis-rsi")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"RSI Analysis", r.data)

    def test_run(self):
        r = self.client.post("/iifl/api/analysis-rsi/run", json=self.body())
        self.assertEqual(r.status_code, 200, r.data)
        res = r.get_json()["result"]
        self.assertIn("holdings", res["result"])
        self.assertIn("plain", res)
        self.assertEqual(set(res["compare"]), set(rb.TIMEFRAMES))

    def test_validation(self):
        r = self.client.post("/iifl/api/analysis-rsi/run", json=self.body(hold_rsi=80, exit_rsi=70))
        self.assertEqual(r.status_code, 400)

    def test_optimize_job(self):
        r = self.client.post("/iifl/api/analysis-rsi/optimize", json=self.body())
        self.assertEqual(r.status_code, 200, r.data)
        job = r.get_json()["result"]["job_id"]
        for _ in range(240):
            j = self.client.get(f"/iifl/api/analysis-rsi/job/{job}").get_json()["result"]
            if j["state"] != "RUNNING":
                break
            time.sleep(0.5)
        self.assertEqual(j["state"], "DONE", j.get("message"))
        self.assertTrue(j["result"]["holdings"])

    def test_now_uses_stored_data_when_market_closed(self):
        r = self.client.post("/iifl/api/analysis-rsi/now", json=self.body())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("rsi", r.get_json()["result"])

    def test_classic_analysis_untouched(self):
        r = self.client.post("/iifl/api/analysis/run", json={"instrumentId": "999", "target_pct": 1, "shares": 10, "max_open": 20})
        self.assertEqual(r.status_code, 200, r.data)


if __name__ == "__main__":
    unittest.main()
