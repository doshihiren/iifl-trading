"""Engine tests against a simulated IIFL broker. Run:  python -m unittest -v tests.test_engine"""

import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp()
os.environ.update({
    "IIFL_ENV_FILE": os.path.join(_TMP, "none.env"),
    "BOT_DB_FILE": os.path.join(_TMP, "t.db"),
    "BOT_DATA_FILE": os.path.join(_TMP, "bots.json"),
    "HOLIDAYS_FILE": os.path.join(_TMP, "holidays.json"),
    "LIVE_TRADING": "true",
    "IIFL_TRADING_IP_AUTHORIZED": "true",
    "LIVE_MAX_QTY_PER_ORDER": "10",
    "GLOBAL_MAX_CAPITAL": "1000000",
})

import broker as real_broker  # noqa: E402
import bot_store  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import engine as eng  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

# Tests simulate mismatches and manual exits on purpose; keep their log lines out of the output.
logging.disable(logging.CRITICAL)


class FakeBroker:
    SessionExpired = real_broker.SessionExpired
    build_order = staticmethod(real_broker.build_order)
    broker_quantity = staticmethod(real_broker.broker_quantity)

    def __init__(self):
        self.placed = []
        self.book = {}
        self.trades = []
        self.next_outcome = "ACCEPTED"
        self.session_valid = True
        self.positions_rows = None   # None = derive from filled orders
        self.holdings_rows = []
        self.auto_fill_price = None   # fill every accepted order at this price

    def check_session(self):
        if not self.session_valid:
            raise real_broker.SessionExpired("session expired")
        return True

    def place_order(self, payload):
        self.placed.append(payload)
        outcome = self.next_outcome
        if outcome != "ACCEPTED":
            if outcome == "UNKNOWN_BUT_PLACED":
                self._add(payload)
                return {"outcome": "UNKNOWN", "message": "timeout", "broker_order_id": None,
                        "http_status": None, "body": None}
            return {"outcome": outcome, "message": "nope", "broker_order_id": None, "http_status": 400, "body": {}}
        boid = self._add(payload)
        return {"outcome": "ACCEPTED", "broker_order_id": boid, "message": "", "http_status": 200,
                "body": {"status": "Ok", "result": [{"brokerOrderId": boid, "status": "Ok"}]}}

    def _add(self, payload):
        p = payload[0]
        boid = str(1000 + len(self.book) + 1)
        self.book[boid] = {
            "broker_order_id": boid, "exchange_order_id": "", "status": "OPEN", "raw_status": "Open",
            "instrument_id": p["instrumentId"], "side": p["transactionType"], "product": p["product"],
            "quantity": int(p["quantity"]), "filled_qty": 0, "avg_price": None, "reject_reason": "",
            "tag": p.get("orderTag", ""), "time": "",
        }
        if self.auto_fill_price is not None:
            self.fill(boid, self.auto_fill_price)
        return boid

    def fill(self, boid, price, parts=None):
        row = self.book[boid]
        row.update(status="COMPLETE", raw_status="Complete", filled_qty=row["quantity"], avg_price=price)
        for i, (q, px) in enumerate(parts or [(row["quantity"], price)]):
            self.trades.append({"broker_order_id": boid, "trade_ref": f"T{boid}-{i}", "qty": q, "price": px, "time": ""})

    def reject(self, boid, reason="RMS: insufficient funds"):
        self.book[boid].update(status="REJECTED", raw_status="Rejected", reject_reason=reason)

    def order_book(self):
        return [dict(r) for r in self.book.values()]

    def trade_book(self):
        return list(self.trades)

    def positions(self):
        if self.positions_rows is not None:
            return self.positions_rows
        net = {}
        for r in self.book.values():
            if r["status"] == "COMPLETE":
                k = (r["instrument_id"], r["product"])
                net[k] = net.get(k, 0) + (r["filled_qty"] if r["side"] == "BUY" else -r["filled_qty"])
        return [{"instrumentId": k[0], "product": k[1], "netQuantity": v} for k, v in net.items()]

    def holdings(self):
        return self.holdings_rows


class Base(unittest.TestCase):
    def setUp(self):
        db.reset_connection()
        config.BOT_DB_FILE = os.path.join(_TMP, os.path.basename(os.environ['BOT_DB_FILE']))
        config.DATA_DIR = _TMP
        config.LIVE_TRADING = True
        config.IIFL_TRADING_IP_AUTHORIZED = True
        config.GLOBAL_MAX_CAPITAL = 1000000.0
        config.HOLIDAYS_FILE = os.path.join(_TMP, "holidays.json")
        config.BOT_DATA_FILE = os.path.join(_TMP, "bots.json")
        db._initialized_paths.clear()
        for f in os.listdir(_TMP):
            os.remove(os.path.join(_TMP, f))
        self.now = datetime(2026, 10, 5, 10, 0, 30, tzinfo=IST)   # Monday
        self.broker = FakeBroker()
        self.prices = {"6792": 59.00}
        self.engine = self.make_engine()

    def make_engine(self):
        def quotes(instruments):
            return {iid: (self.prices.get(iid), None, None) for iid, _ in instruments}
        e = eng.Engine(broker=self.broker, quotes=quotes, now_fn=lambda: self.now)
        e.startup()
        return e

    def restart(self):
        db.reset_connection()
        self.engine = self.make_engine()

    def add_bot(self, mode="LIVE", qty=1, target=1.0, timeframe="1m", product="DELIVERY", **kw):
        b = bot_store.add_bot({"batch_size": kw.get("batch_size", 0), "rebuy_dip_pct": kw.get("dip", 0),"symbol": "SBC", "tradingSymbol": "SBC-EQ", "instrumentId": "6792",
                               "exchange": "NSEEQ", "tick_size": 0.01, "mode": mode, "qty": qty,
                               "target": target, "timeframe": timeframe, "product": product,
                               "maxpos": kw.get("maxpos", 50), "capital": kw.get("capital", 100000),
                               "status": "RUNNING"})
        return b["bot_id"]

    def tick(self, seconds=5):
        self.engine.run_once()
        self.now += timedelta(seconds=seconds)

    def lots(self, bot_id=None, status=None):
        sql, p = "SELECT * FROM trades WHERE 1=1", []
        if bot_id:
            sql += " AND bot_id=?"; p.append(bot_id)
        if status:
            sql += " AND status=?"; p.append(status)
        return db.rows(sql, p)


class TestHelpers(unittest.TestCase):
    def test_target_rounds_up_to_tick(self):
        self.assertEqual(eng.target_from_fill(59.00, 1.0, 0.01), 59.59)
        self.assertEqual(eng.target_from_fill(59.03, 1.0, 0.01), 59.63)   # 59.6203 -> 59.63
        self.assertEqual(eng.target_from_fill(1234.5, 0.5, 0.05), 1240.70)  # 1240.6725 -> 1240.70
        self.assertEqual(eng.target_from_fill(58.0, 1.0, 0.01), 58.58)

    def test_qty_range(self):
        self.assertEqual(eng.qty_range(125, 10), (113, 137))
        self.assertEqual(eng.qty_range(1, 10), (1, 1))
        self.assertEqual(eng.qty_range(10, 10), (9, 11))
        self.assertEqual(eng.qty_range(50, 0), (50, 50))

    def test_slots(self):
        t = datetime(2026, 10, 5, 10, 7, 12, tzinfo=IST)
        self.assertEqual(eng.current_slot("1m", t), "2026-10-05T10:07")
        self.assertEqual(eng.current_slot("5m", t), "2026-10-05T10:05")
        self.assertEqual(eng.current_slot("15m", t), "2026-10-05T10:00")

    def test_broker_status_mapping(self):
        self.assertEqual(real_broker.map_order_status("Complete"), "COMPLETE")
        self.assertEqual(real_broker.map_order_status("rejected"), "REJECTED")
        self.assertEqual(real_broker.map_order_status("Put Order Req Received"), "OPEN")

    def test_place_order_parsing(self):
        class R:
            def __init__(self, code, body):
                self.status_code, self._b, self.text = code, body, json.dumps(body)
            def json(self):
                return self._b
        import requests
        orig_post, orig_hdr = requests.post, real_broker._headers
        real_broker._headers = lambda: {}
        try:
            requests.post = lambda *a, **k: R(200, {"status": "Ok", "result": [{"brokerOrderId": "250930000123", "status": "Ok"}]})
            self.assertEqual(real_broker.place_order([{}])["outcome"], "ACCEPTED")
            requests.post = lambda *a, **k: R(400, {"status": "error", "message": "Invalid quantity"})
            self.assertEqual(real_broker.place_order([{}])["outcome"], "REJECTED")
            requests.post = lambda *a, **k: R(401, {"status": "error", "message": "Invalid session"})
            self.assertEqual(real_broker.place_order([{}])["outcome"], "SESSION")
            requests.post = lambda *a, **k: R(500, {"message": "boom"})
            self.assertEqual(real_broker.place_order([{}])["outcome"], "UNKNOWN")
            requests.post = lambda *a, **k: R(200, {"status": "error", "message": "Something went wrong, please try after some time"})
            self.assertEqual(real_broker.place_order([{}])["outcome"], "UNKNOWN")
            def boom(*a, **k):
                raise requests.exceptions.ReadTimeout()
            requests.post = boom
            self.assertEqual(real_broker.place_order([{}])["outcome"], "UNKNOWN")
        finally:
            requests.post, real_broker._headers = orig_post, orig_hdr


class TestMigration(Base):
    def test_json_migration(self):
        db.reset_connection(); db._initialized_paths.clear()
        os.remove(config.BOT_DB_FILE)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(config.BOT_DB_FILE + suffix):
                os.remove(config.BOT_DB_FILE + suffix)
        with open(config.BOT_DATA_FILE, "w") as f:
            json.dump({"bots": [
                {"bot_id": "a1", "symbol": "SBC", "instrumentId": "6792", "mode": "LIVE", "qty": 1,
                 "target": 1.0, "timeframe": "1m", "status": "ACTION_REQUIRED",
                 "pending_order": {"side": "BUY"}},
                {"bot_id": "p1", "symbol": "SBC", "instrumentId": "6792", "mode": "PAPER", "status": "RUNNING"}],
                "trades": [
                {"id": 1, "bot_id": "p1", "symbol": "SBC", "quantity": 1, "entry_price": 59.0,
                 "target_price": 59.59, "mode": "PAPER", "status": "OPEN"},
                {"id": 2, "bot_id": "p1", "symbol": "SBC", "quantity": 1, "entry_price": 58.0,
                 "target_price": 58.58, "mode": "PAPER", "status": "CLOSED", "exit_price": 58.6, "pnl": 0.6}]}, f)
        bots = {b["bot_id"]: b for b in bot_store.list_bots()}
        self.assertEqual(bots["a1"]["status"], "STOPPED")
        self.assertEqual(bots["p1"]["status"], "RUNNING")
        self.assertEqual(len(bot_store.list_trades()), 2)
        self.assertEqual(len(bot_store.open_trades(bot_id="p1")), 1)
        self.assertTrue(any(n.endswith(".bak") for n in os.listdir(_TMP)))
        self.assertEqual(db.migrate_from_json()["status"], "already_migrated")


class TestPaper(Base):
    def test_paper_entry_and_exit(self):
        bid = self.add_bot(mode="PAPER")
        self.tick()
        lots = self.lots(bid)
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["target_price"], 59.59)
        self.assertEqual(self.broker.placed, [])
        self.prices["6792"] = 59.60
        self.tick()
        self.assertEqual(self.lots(bid, "CLOSED")[0]["pnl"], 0.6)


class TestLive(Base):
    def test_buy_uses_actual_fill_and_ceil_target(self):
        bid = self.add_bot()
        self.tick()
        self.assertEqual(len(self.broker.placed), 1)
        self.assertEqual(self.lots(bid), [])            # accepted is NOT a trade
        o = db.row("SELECT * FROM orders")
        self.assertEqual(o["state"], "SUBMITTED")
        self.broker.fill(o["broker_order_id"], 59.03)
        self.tick()
        lot = self.lots(bid)[0]
        self.assertEqual(lot["entry_price"], 59.03)
        self.assertEqual(lot["target_price"], 59.63)
        self.assertEqual(lot["broker_buy_order_id"], o["broker_order_id"])

    def test_vwap_from_trade_book(self):
        bid = self.add_bot(qty=3)
        self.tick()
        o = db.row("SELECT * FROM orders")
        self.broker.fill(o["broker_order_id"], 59.0, parts=[(1, 59.00), (2, 59.03)])
        self.tick()
        self.assertAlmostEqual(self.lots(bid)[0]["entry_price"], 59.02, places=6)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM fills"), 2)

    def test_no_duplicate_in_same_slot_or_after_restart(self):
        self.add_bot()
        self.tick(1); self.tick(1); self.tick(1)
        self.assertEqual(len(self.broker.placed), 1)
        self.restart()
        self.tick(1)
        self.assertEqual(len(self.broker.placed), 1)
        self.now += timedelta(minutes=1)
        self.broker.fill(db.row("SELECT * FROM orders")["broker_order_id"], 59.0)
        self.tick()
        self.assertEqual(len(self.broker.placed), 2)   # next slot -> one new order

    def test_crash_while_submitting_is_matched_not_resent(self):
        bid = self.add_bot()
        self.broker.next_outcome = "UNKNOWN_BUT_PLACED"
        self.tick()
        self.assertEqual(db.row("SELECT state FROM orders")["state"], "UNKNOWN")
        self.restart()
        self.broker.next_outcome = "ACCEPTED"
        self.tick()
        o = db.row("SELECT * FROM orders")
        self.assertEqual(o["state"], "SUBMITTED")
        self.assertEqual(len(self.broker.placed), 1)
        self.broker.fill(o["broker_order_id"], 59.0)
        self.tick()
        self.assertEqual(len(self.lots(bid)), 1)

    def test_submitting_state_on_startup_becomes_unknown(self):
        bid = self.add_bot()
        bot = bot_store.get_bot(bid)
        with db.tx() as c:
            self.engine._insert_order(c, order_key="X", bot=bot, side="BUY", qty=1, ref=59, signal_key="X",
                                      lot_id=None, state="SUBMITTING")
        self.restart()
        self.assertEqual(db.row("SELECT state FROM orders WHERE order_key='X'")["state"], "UNKNOWN")

    def test_unknown_not_found_times_out_without_resend(self):
        self.add_bot()
        self.broker.next_outcome = "UNKNOWN"
        self.tick()
        self.now += timedelta(seconds=config.UNKNOWN_ORDER_TIMEOUT_SECONDS + 5)
        self.broker.next_outcome = "ACCEPTED"
        self.tick()
        states = [r["state"] for r in db.rows("SELECT state FROM orders ORDER BY id")]
        self.assertEqual(states[0], "REJECTED")

    def test_rejected_buy_creates_no_trade(self):
        bid = self.add_bot()
        self.tick()
        self.broker.reject(db.row("SELECT * FROM orders")["broker_order_id"])
        self.tick()
        self.assertEqual(self.lots(bid), [])
        self.assertIn("insufficient", bot_store.get_bot(bid)["last_error"])

    def test_same_symbol_bots_keep_separate_lots(self):
        a = self.add_bot(qty=1, target=1.0, timeframe="1m")
        b = self.add_bot(qty=5, target=0.5, timeframe="5m")
        self.broker.auto_fill_price = 58.0
        self.tick()
        self.broker.auto_fill_price = 58.10
        self.prices["6792"] = 58.10
        self.now += timedelta(minutes=5)   # new slot for both
        self.tick()
        self.tick()
        la, lb = self.lots(a, "OPEN"), self.lots(b, "OPEN")
        self.assertEqual([x["quantity"] for x in lb], [5, 5])
        self.assertEqual(lb[0]["target_price"], 58.29)
        self.assertEqual(la[0]["target_price"], 58.58)
        # Broker holds the aggregate; price hits only bot B's 58.29 target.
        self.engine.last_recon = None
        self.broker.auto_fill_price = 58.30
        self.prices["6792"] = 58.30
        self.tick(); self.tick()
        closed = self.lots(status="CLOSED")
        self.assertEqual({c["bot_id"] for c in closed}, {b})
        self.assertEqual(closed[0]["quantity"], 5)
        self.assertEqual(closed[0]["pnl"], 1.5)
        self.assertTrue(all(l["status"] == "OPEN" for l in self.lots(a)))

    def test_sell_rejected_returns_lot_to_open_and_retries(self):
        bid = self.add_bot()
        self.broker.auto_fill_price = 59.0
        self.tick()
        self.broker.auto_fill_price = None
        self.prices["6792"] = 60.0
        self.tick()
        sell = db.row("SELECT * FROM orders WHERE side='SELL'")
        self.broker.reject(sell["broker_order_id"], "RMS: no holdings")
        self.tick()
        lot = self.lots(bid)[0]
        self.assertEqual(lot["status"], "OPEN")
        self.now += timedelta(seconds=config.EXIT_RETRY_SECONDS + 1)
        self.tick()
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM orders WHERE side='SELL'"), 2)

    def test_intraday_sell_held_when_broker_shows_no_shares(self):
        bid = self.add_bot(product="INTRADAY")
        self.broker.auto_fill_price = 59.0
        self.tick()
        self.broker.positions_rows = []    # broker shows nothing
        self.engine.last_recon = None
        self.prices["6792"] = 60.0
        self.tick()
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM orders WHERE side='SELL'"), 0)
        self.assertIn("sellable", bot_store.get_bot(bid)["last_error"])

    def test_session_expired_blocks_orders(self):
        self.add_bot()
        self.broker.session_valid = False
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.assertEqual(db.get_runtime("broker_session")["state"], "EXPIRED")

    def test_holiday_no_trading(self):
        self.add_bot()
        self.now = datetime(2026, 10, 2, 10, 0, tzinfo=IST)   # Gandhi Jayanti
        self.tick()
        self.assertEqual(self.broker.placed, [])

    def test_random_qty_within_ten_percent(self):
        bid = self.add_bot(qty=125, capital=10_000_000, maxpos=500)
        self.broker.auto_fill_price = 59.0
        seen = set()
        for _ in range(40):
            self.tick()
            self.now += timedelta(minutes=1)
        for payload in self.broker.placed:
            q = int(payload[0]["quantity"])
            self.assertTrue(113 <= q <= 137, q)
            seen.add(q)
        self.assertGreater(len(seen), 5)          # really varies
        lots = self.lots(bid)
        filled = db.rows("SELECT filled_qty FROM orders WHERE side='BUY' AND state='FILLED'")
        self.assertEqual(sorted(l["quantity"] for l in lots), sorted(o["filled_qty"] for o in filled))

    def test_capital_limit_is_the_size_brake(self):
        bid = self.add_bot(qty=125, capital=5000)    # 125 x 59 = 7375 > 5000
        self.tick()
        self.assertEqual(self.broker.placed, [])
        self.assertIn("capital", bot_store.get_bot(bid)["last_error"])

    def test_stop_keeps_managing_exits(self):
        bid = self.add_bot()
        self.broker.auto_fill_price = 59.0
        self.tick()
        bot_store.update_bot(bid, status="STOPPED")
        self.now += timedelta(minutes=1)
        self.tick()
        self.assertEqual(len(self.broker.placed), 1)      # no new BUY
        self.prices["6792"] = 59.60
        self.tick(); self.tick()
        self.assertEqual(len(self.lots(bid, "CLOSED")), 1)  # exit still happened

    def test_exit_and_stop(self):
        bid = self.add_bot()
        self.broker.auto_fill_price = 59.0
        self.tick()
        bot_store.update_bot(bid, status="EXITING")
        self.tick(); self.tick()
        self.assertEqual(len(self.lots(bid, "CLOSED")), 1)
        self.assertEqual(bot_store.get_bot(bid)["status"], "STOPPED")

    def test_max_positions(self):
        bid = self.add_bot(maxpos=2)
        self.broker.auto_fill_price = 59.0
        for _ in range(4):
            self.tick()
            self.now += timedelta(minutes=1)
        self.assertEqual(len(self.lots(bid, "OPEN")), 2)


if __name__ == "__main__":
    unittest.main()


class TestManualExit(Base):
    def setup_lots(self, n=2, qty=1):
        bid = self.add_bot(qty=qty)
        self.broker.auto_fill_price = 59.0
        for _ in range(n):
            self.tick(); self.tick()
            self.now += timedelta(minutes=1)
        bot_store.update_bot(bid, status="STOPPED")
        return bid

    def recon_ticks(self, n=4):
        for _ in range(n):
            self.engine.last_recon = None
            self.tick(30)

    def test_manual_exit_uses_trade_book_price(self):
        bid = self.setup_lots(2)
        self.broker.positions_rows = []                      # user sold both in IIFL app
        self.broker.trades.append({"broker_order_id": "MAN1", "trade_ref": "X1", "qty": 2, "price": 59.80,
                                   "time": "", "instrument_id": "6792", "side": "SELL", "product": "DELIVERY"})
        self.recon_ticks()
        lots = self.lots(bid)
        self.assertEqual([l["status"] for l in lots], ["CLOSED", "CLOSED"])
        self.assertEqual({l["exit_reason"] for l in lots}, {"MANUAL"})
        self.assertEqual(lots[0]["exit_price"], 59.80)
        self.assertEqual(lots[0]["pnl"], 0.8)
        self.assertIn("trade book", lots[0]["exit_note"])
        self.assertIsNone(bot_store.get_bot(bid)["last_error"])
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM orders WHERE side='SELL'"), 0)

    def test_manual_exit_without_price_uses_ltp(self):
        bid = self.setup_lots(1)
        self.prices["6792"] = 58.70
        self.broker.positions_rows = []
        self.recon_ticks()
        lot = self.lots(bid)[0]
        self.assertEqual((lot["status"], lot["exit_price"]), ("CLOSED", 58.70))
        self.assertIn("estimated", lot["exit_note"])

    def test_partial_manual_exit_splits_lot(self):
        bid = self.setup_lots(1, qty=3)
        self.broker.positions_rows = [{"instrumentId": "6792", "product": "DELIVERY", "netQuantity": 1}]
        self.recon_ticks()
        closed = self.lots(bid, "CLOSED"); still = self.lots(bid, "OPEN")
        self.assertEqual((closed[0]["quantity"], still[0]["quantity"]), (2, 1))

    def test_no_false_alarm_while_buy_pending(self):
        bid = self.add_bot(qty=1)
        self.tick()                                         # BUY submitted, not filled
        self.broker.positions_rows = []
        self.recon_ticks(6)
        self.assertEqual(db.scalar("SELECT COUNT(*) FROM trades WHERE exit_reason='MANUAL'"), 0)

    def test_needs_several_checks(self):
        bid = self.setup_lots(1)
        self.broker.positions_rows = []
        self.recon_ticks(1)
        self.assertEqual(self.lots(bid)[0]["status"], "OPEN")


class TestBatch(Base):
    def buys(self):
        return db.scalar("SELECT COUNT(*) FROM orders WHERE side='BUY' AND state='FILLED'")

    def run_minutes(self, n):
        for _ in range(n):
            self.tick(); self.tick()
            self.now += timedelta(minutes=1)

    def test_batch_pause_and_rebuy_after_dip(self):
        bid = self.add_bot(mode="PAPER", target=1.0, batch_size=3, dip=2.0)
        self.run_minutes(6)
        self.assertEqual(self.buys(), 3)                       # stopped after 3
        self.assertEqual(bot_store.get_bot(bid)["batch_state"], "WAITING_SELL")
        self.prices["6792"] = 60.00                            # all 3 lots (target 59.59) sell
        self.run_minutes(1)
        bot = bot_store.get_bot(bid)
        self.assertEqual(bot["batch_state"], "WAITING_DIP")
        self.assertEqual(bot["rebuy_ref_price"], 60.0)
        self.assertEqual(bot["rebuy_trigger_price"], 58.80)    # 60 - 2%
        self.prices["6792"] = 59.00                            # only -1.7%: still waiting
        self.run_minutes(3)
        self.assertEqual(self.buys(), 3)
        self.prices["6792"] = 58.80                            # -2%: new batch
        self.run_minutes(5)
        self.assertEqual(self.buys(), 6)
        bot = bot_store.get_bot(bid)
        self.assertEqual((bot["batch_no"], bot["batch_state"]), (2, "WAITING_SELL"))

    def test_first_buy_of_new_batch_is_immediate(self):
        bid = self.add_bot(mode="PAPER", timeframe="15m", batch_size=1, dip=1.0)
        self.tick()
        self.assertEqual(self.buys(), 1)
        self.prices["6792"] = 59.60
        self.tick(); self.tick()
        self.prices["6792"] = 59.00                            # trigger 59.60*0.99=59.00
        self.tick()
        self.assertEqual(self.buys(), 2)                       # same 15m slot, new batch

    def test_batch_survives_restart_live(self):
        bid = self.add_bot(batch_size=2, dip=1.0)
        self.broker.auto_fill_price = 59.0
        self.run_minutes(4)
        self.assertEqual(len(self.broker.placed), 2)
        self.restart()
        self.run_minutes(3)
        self.assertEqual(len(self.broker.placed), 2)           # still paused after restart

    def test_zero_batch_size_keeps_old_behaviour(self):
        self.add_bot(mode="PAPER")
        self.run_minutes(5)
        self.assertEqual(self.buys(), 5)


class TestReports(Base):
    def test_live_only_with_open_and_manual(self):
        import reports
        live = self.add_bot(mode="LIVE")
        paper = self.add_bot(mode="PAPER")
        def mk(bot_id, mode, entry, exit_=None, qty=1, reason="TARGET"):
            t = bot_store.add_trade({"bot_id": bot_id, "symbol": "SBC", "quantity": qty, "entry_price": entry,
                                     "target_price": round(entry * 1.01, 2), "mode": mode})
            if exit_ is not None:
                bot_store.close_trade(t["id"], exit_price=exit_, exit_reason=reason)
        mk(live, "LIVE", 59.0, 59.59)
        mk(live, "LIVE", 60.0, 59.0, qty=2, reason="MANUAL")
        mk(live, "LIVE", 58.0)                       # still open
        mk(paper, "PAPER", 58.0, 58.58, qty=5)       # must not appear
        bot_store.update_bot(live, last_ltp=58.5)
        today = datetime.now(IST).date().isoformat()
        r = reports.build({"from": today, "to": today})
        self.assertEqual(r["summary"]["trades"], 2)
        self.assertEqual(r["summary"]["gross_pnl"], round(0.59 - 2.0, 2))
        self.assertEqual(r["entries"]["count"], 3)
        self.assertEqual(len(r["trades"]), 3)
        labels = sorted(t["status_label"] for t in r["trades"])
        self.assertEqual(labels, ["Exited manually (IIFL)", "Open", "Target hit"])
        opened = [t for t in r["trades"] if t["status"] == "OPEN"][0]
        self.assertEqual(opened["unrealized"], 0.5)
        self.assertEqual({e["key"] for e in r["exits"]}, {"Target hit", "Exited manually (IIFL)"})
        manual = reports.build({"from": today, "to": today, "status": "MANUAL"})
        self.assertEqual(len(manual["trades"]), 1)
        self.assertIn("status_label", reports.to_csv(r).splitlines()[0])
        old = reports.build({"from": "2026-01-01", "to": "2026-01-31"})
        self.assertEqual(old["summary"]["trades"], 0)
        self.assertEqual(len(old["trades"]), 1)      # open trade is always listed
