"""Trading engine shared by PAPER and LIVE bots.

Life of a LIVE entry:
    slot reached -> strategy_signals row (unique per bot+slot)
                 -> orders row SUBMITTING (unique order_key) in the same transaction
                 -> POST /v1/orders
                 -> SUBMITTED | REJECTED | UNKNOWN
                 -> order book polled until COMPLETE / REJECTED / CANCELLED
                 -> FILLED: trade lot OPEN at the ACTUAL average fill price,
                    target = fill * (1 + target%) rounded UP to the tick size
Life of an exit:
    LTP >= lot target -> SELL order (key lot|SELL|attempt), lot EXIT_SUBMITTED
                      -> SELL FILLED: lot CLOSED with P&L from actual fills
                      -> SELL rejected: lot back to OPEN, retried later

A submitted order is never treated as a trade. An order whose outcome is not
known (timeout, crash between insert and response) is never re-sent: it is
matched against the IIFL order book instead.

PAPER bots run through exactly the same tables; their "broker" fills every
order instantly at the current LTP.
"""

import hashlib
import json
import logging
import math
import random
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from zoneinfo import ZoneInfo

import broker as broker_mod
import config
import db
import market_calendar as cal

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("iifl.engine")

ACTIVE_ORDER_STATES = ("SUBMITTING", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN")
LOT_HELD_STATES = ("OPEN", "EXIT_SUBMITTED")


# ---- helpers -------------------------------------------------------------

def ceil_to_tick(price, tick):
    p = Decimal(str(price))
    t = Decimal(str(tick or 0.05))
    steps = (p / t).to_integral_value(rounding=ROUND_CEILING)
    return float(steps * t)


def target_from_fill(fill_price, target_pct, tick):
    raw = Decimal(str(fill_price)) * (Decimal(1) + Decimal(str(target_pct)) / Decimal(100))
    return ceil_to_tick(raw, tick)


_rng = random.SystemRandom()


def qty_range(base_qty, random_pct):
    """Smallest and largest quantity for base_qty +/- random_pct percent (never below 1)."""
    base = int(base_qty)
    pct = max(0.0, float(random_pct or 0))
    low = max(1, math.ceil(base * (1 - pct / 100.0) - 1e-9))
    high = max(low, math.floor(base * (1 + pct / 100.0) + 1e-9))
    return low, high


def randomized_qty(base_qty, random_pct, rng=None):
    low, high = qty_range(base_qty, random_pct)
    return (rng or _rng).randint(low, high)


def order_tag(order_key):
    return "BOT" + hashlib.sha1(order_key.encode()).hexdigest()[:12].upper()


def timeframe_minutes(value):
    return {"1m": 1, "5m": 5, "15m": 15}.get(str(value).lower(), 15)


def current_slot(timeframe, now):
    start = 9 * 60 + 15
    total = now.hour * 60 + now.minute
    if total < start:
        return None
    step = timeframe_minutes(timeframe)
    slot = start + ((total - start) // step) * step
    hour, minute = divmod(slot, 60)
    return now.strftime("%Y-%m-%d") + f"T{hour:02d}:{minute:02d}"


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=IST)
    except ValueError:
        return None


def _money(v):
    return round(float(v), 2)


def elog(kind, message, *, level="INFO", bot_id=None, store=True, **fields):
    parts = [f"event={kind}"]
    if bot_id:
        parts.append(f"bot_id={bot_id}")
    parts += [f"{k}={v}" for k, v in fields.items() if v is not None]
    log.log(getattr(logging, level, logging.INFO), "%s | %s", " ".join(parts), message)
    if store:
        db.add_event(kind, message, level=level, bot_id=bot_id, data=fields or None)


# ---- quotes --------------------------------------------------------------

def fetch_quotes(instruments):
    """instruments: list of (instrument_id, exchange). Returns {iid: (ltp, quote_time)}."""
    import market_data
    out = {}
    if not instruments:
        return out
    for iid, exchange in instruments:
        status, body = market_data.market_quote(iid, exchange=exchange)
        if broker_mod.is_session_error(status, body):
            raise broker_mod.SessionExpired(broker_mod.message_of(body) or f"HTTP {status}")
        if status != 200:
            out[str(iid)] = (None, None, f"Quote HTTP {status}")
            continue
        try:
            item = body["result"][0]
            ltp = float(item["ltp"])
        except Exception:
            out[str(iid)] = (None, None, "LTP missing in quote")
            continue
        qtime = item.get("tickTimestamp") or item.get("lastTradedTime") or item.get("exchangeTimestamp")
        out[str(iid)] = (ltp, qtime, None)
    return out


# ---- engine --------------------------------------------------------------

class Engine:
    def __init__(self, broker=broker_mod, quotes=fetch_quotes, now_fn=None):
        self.broker = broker
        self.quotes = quotes
        self.now_fn = now_fn or (lambda: datetime.now(IST))
        self.last_session_check = None
        self.session_ok = None
        self.last_recon = None
        self.snapshot = {}          # (instrumentId, product) -> broker qty
        self.snapshot_at = None
        self.mismatch_counts = {}

    def now(self):
        return self.now_fn()

    # ---------- startup ----------
    def startup(self):
        with db.tx() as c:
            stuck = c.execute(
                "SELECT id, order_key, bot_id FROM orders WHERE state='SUBMITTING'"
            ).fetchall()
            for o in stuck:
                c.execute(
                    "UPDATE orders SET state='UNKNOWN', reject_reason=?, updated_at=? WHERE id=?",
                    ("worker restarted while submitting; resolving from order book", db.now_utc(), o["id"]),
                )
        for o in stuck:
            elog("ORDER_RECOVERY", "Order outcome unknown after restart; will match against IIFL order book",
                 level="WARNING", bot_id=o["bot_id"], order_key=o["order_key"])
        elog("WORKER_START", "Trading worker started", store=True)

    # ---------- session ----------
    def set_session(self, state, detail=""):
        prev = db.get_runtime("broker_session", {}).get("state")
        db.set_runtime("broker_session", {"state": state, "detail": detail[:300],
                                          "checked_at": db.now_utc()})
        if prev != state:
            elog("BROKER_SESSION", f"IIFL session {state} {detail}".strip(),
                 level="INFO" if state == "CONNECTED" else "ERROR")
        self.session_ok = state == "CONNECTED"

    def ensure_session(self, now):
        interval = config.SESSION_CHECK_SECONDS if self.session_ok else 15
        due = (self.last_session_check is None
               or (now - self.last_session_check).total_seconds() >= interval)
        if not due:
            return self.session_ok
        self.last_session_check = now
        try:
            self.broker.check_session()
            self.set_session("CONNECTED")
        except self.broker.SessionExpired as exc:
            self.set_session("EXPIRED", str(exc))
        except Exception as exc:
            # Network trouble is not an expired session; keep previous view.
            log.warning("event=SESSION_CHECK_FAILED error=%s", exc)
            if self.session_ok is None:
                self.session_ok = False
        return self.session_ok

    # ---------- main loop ----------
    def run_once(self):
        now = self.now()
        db.set_runtime("worker_heartbeat", {"at": now.isoformat()})
        status = cal.market_status(now)
        db.set_runtime("market_status", {"state": status, "at": now.isoformat()})

        bots = db.rows("SELECT * FROM bots WHERE deleted=0")
        live_work = self._has_live_work(bots)
        any_work = live_work or self._has_paper_work(bots)

        trading_day = cal.is_trading_day(now)
        after_hours_sync = trading_day and cal.parse_hhmm("09:00") <= now.time() < cal.parse_hhmm("16:00")

        if not any_work:
            return
        if status != "OPEN" and not (live_work and after_hours_sync):
            return

        if not self.ensure_session(now):
            return

        try:
            if live_work:
                self.sync_live_orders(now)
                if self.last_recon is None or (now - self.last_recon).total_seconds() >= config.RECON_SECONDS:
                    self.reconcile_positions(now)
            if status != "OPEN":
                return

            active = [b for b in bots if self._bot_needs_tick(b)]
            instruments = sorted({(str(b["instrumentId"]), b["exchange"]) for b in active})
            quotes = self.quotes(instruments)
            for bot in active:
                try:
                    self.process_bot(bot, quotes.get(str(bot["instrumentId"])), now)
                except self.broker.SessionExpired:
                    raise
                except Exception as exc:
                    log.exception("event=BOT_ERROR bot_id=%s", bot["bot_id"])
                    self._bot_error(bot["bot_id"], str(exc))
        except self.broker.SessionExpired as exc:
            self.set_session("EXPIRED", str(exc))

    def _has_live_work(self, bots):
        if any(b["mode"] == "LIVE" and b["status"] in ("RUNNING", "EXITING") for b in bots):
            return True
        n = db.scalar("SELECT COUNT(*) FROM trades WHERE mode='LIVE' AND status IN ('OPEN','EXIT_SUBMITTED')")
        if n:
            return True
        n = db.scalar(
            f"SELECT COUNT(*) FROM orders WHERE mode='LIVE' AND state IN ({','.join('?'*len(ACTIVE_ORDER_STATES))})",
            ACTIVE_ORDER_STATES)
        return bool(n)

    def _has_paper_work(self, bots):
        if any(b["mode"] == "PAPER" and b["status"] in ("RUNNING", "EXITING") for b in bots):
            return True
        return bool(db.scalar("SELECT COUNT(*) FROM trades WHERE mode='PAPER' AND status='OPEN'"))

    def _bot_needs_tick(self, bot):
        if bot["status"] in ("RUNNING", "EXITING"):
            return True
        return bool(db.scalar(
            "SELECT COUNT(*) FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')",
            (bot["bot_id"],)))

    def _bot_error(self, bot_id, message):
        with db.tx() as c:
            c.execute("UPDATE bots SET last_error=?, updated_at=? WHERE bot_id=?",
                      (message[:500], db.now_utc(), bot_id))

    # ---------- per bot ----------
    def process_bot(self, bot, quote, now):
        bot_id = bot["bot_id"]
        if not quote or quote[0] is None:
            self._bot_error(bot_id, (quote[2] if quote else None) or "No quote received")
            return
        ltp, qtime, _ = quote

        stale = False
        qdt = cal.parse_quote_time(qtime)
        if qdt is not None and (now - qdt).total_seconds() > config.QUOTE_MAX_AGE_SECONDS:
            stale = True

        with db.tx() as c:
            c.execute("UPDATE bots SET last_ltp=?, last_tick=?, updated_at=? WHERE bot_id=?",
                      (ltp, now.isoformat(), db.now_utc(), bot_id))
        if stale:
            self._bot_error(bot_id, f"Stale quote (exchange time {qdt:%H:%M:%S}); trading paused")
            return

        self.manage_exits(bot, ltp, now)

        fresh = db.row("SELECT * FROM bots WHERE bot_id=?", (bot_id,))
        if fresh["status"] == "EXITING":
            self._maybe_finish_exiting(fresh)
        elif fresh["status"] == "RUNNING":
            self.maybe_enter(fresh, ltp, now)

    def _maybe_finish_exiting(self, bot):
        held = db.scalar("SELECT COUNT(*) FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')",
                         (bot["bot_id"],))
        pending = db.scalar(
            f"SELECT COUNT(*) FROM orders WHERE bot_id=? AND state IN ({','.join('?'*len(ACTIVE_ORDER_STATES))})",
            (bot["bot_id"], *ACTIVE_ORDER_STATES))
        if not held and not pending:
            with db.tx() as c:
                c.execute("UPDATE bots SET status='STOPPED', updated_at=? WHERE bot_id=? AND status='EXITING'",
                          (db.now_utc(), bot["bot_id"]))
            elog("BOT_STOPPED", "All positions exited; bot stopped", bot_id=bot["bot_id"])

    # ---------- live gates ----------
    def live_block_reason(self, bot, qty):
        if not config.LIVE_TRADING:
            return "LIVE_TRADING is false in .env"
        if not config.IIFL_TRADING_IP_AUTHORIZED:
            return "IIFL_TRADING_IP_AUTHORIZED is false in .env"
        if not self.session_ok:
            return "IIFL session not connected"
        return None

    def _mismatched(self, bot):
        key = f"{bot['instrumentId']}|{bot['product']}"
        return self.mismatch_counts.get(key, 0) >= 2

    # ---------- entries ----------
    def maybe_enter(self, bot, ltp, now):
        bot_id = bot["bot_id"]
        slot = current_slot(bot["timeframe"], now)
        if not slot or bot.get("last_entry_slot") == slot:
            return
        if bot["product"] == "INTRADAY" and cal.at_or_after(now, config.INTRADAY_LAST_ENTRY):
            return
        if not cal.calendar_known(now):
            self._bot_error(bot_id, f"No NSE holiday calendar for {now.year}; add it to holidays.json")
            return

        mode = bot["mode"]
        base_qty = int(bot["qty"])
        qty = randomized_qty(base_qty, bot.get("qty_random_pct", 10))
        signal_key = f"{bot_id}|{slot}|BUY"
        order_key = signal_key

        skip = None
        pending_buys = db.rows(
            f"SELECT * FROM orders WHERE bot_id=? AND side='BUY' AND state IN ({','.join('?'*len(ACTIVE_ORDER_STATES))})",
            (bot_id, *ACTIVE_ORDER_STATES))
        if pending_buys:
            skip = "Previous BUY order still pending"
        if not skip and mode == "LIVE":
            skip = self.live_block_reason(bot, qty)
            if not skip and self._mismatched(bot):
                skip = "Broker quantity is lower than bot positions; new entries paused"
        if not skip:
            lots = db.rows("SELECT * FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')", (bot_id,))
            if len(lots) >= int(bot["maxpos"]):
                skip = "Max open positions reached"
            else:
                estimate = ltp * qty
                bot_capital = sum(float(t["entry_price"]) * int(t["quantity"]) for t in lots)
                if bot_capital + estimate > float(bot["capital"]):
                    skip = "Bot capital limit reached"
                else:
                    global_capital = self.deployed_capital(mode)
                    if global_capital + estimate > config.GLOBAL_MAX_CAPITAL:
                        skip = "Global capital limit reached"

        ts = db.now_utc()
        with db.tx() as c:
            st = c.execute("SELECT status FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
            if not st or st["status"] != "RUNNING":
                return
            cur = c.execute(
                "INSERT OR IGNORE INTO strategy_signals(signal_key, bot_id, slot, side, ltp, decision, detail, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (signal_key, bot_id, slot, "BUY", ltp, "SKIPPED" if skip else "ORDERED", skip, ts))
            if cur.rowcount == 0:
                # Signal already handled (e.g. before a restart) - never act twice.
                c.execute("UPDATE bots SET last_entry_slot=? WHERE bot_id=?", (slot, bot_id))
                return
            c.execute("UPDATE bots SET last_entry_slot=?, last_error=?, updated_at=? WHERE bot_id=?",
                      (slot, skip, ts, bot_id))
            if skip:
                log.info("event=SIGNAL_SKIPPED bot_id=%s slot=%s ltp=%s reason=%s", bot_id, slot, ltp, skip)
                return

            order_id = self._insert_order(c, order_key=order_key, bot=bot, side="BUY", qty=qty,
                                          ref=ltp, signal_key=signal_key, lot_id=None,
                                          state="FILLED" if mode == "PAPER" else "SUBMITTING")
            if mode == "PAPER":
                self._apply_fill(c, order_id, qty, ltp, [])

        elog("SIGNAL", f"BUY signal {bot['symbol']} qty {qty} (base {base_qty}) @~{ltp}", bot_id=bot_id,
             symbol=bot["symbol"], slot=slot, ltp=ltp, qty=qty, base_qty=base_qty, mode=mode,
             store=(mode == "LIVE"))
        if mode == "LIVE":
            self._send(order_id)

    def deployed_capital(self, mode):
        held = db.scalar(
            "SELECT COALESCE(SUM(entry_price*quantity),0) FROM trades WHERE mode=? AND status IN ('OPEN','EXIT_SUBMITTED')",
            (mode,)) or 0.0
        pending = db.scalar(
            f"SELECT COALESCE(SUM(reference_price*qty),0) FROM orders WHERE mode=? AND side='BUY' "
            f"AND state IN ({','.join('?'*len(ACTIVE_ORDER_STATES))})",
            (mode, *ACTIVE_ORDER_STATES)) or 0.0
        return float(held) + float(pending)

    def _insert_order(self, c, *, order_key, bot, side, qty, ref, signal_key, lot_id, state):
        ts = db.now_utc()
        tag = order_tag(order_key) if (bot["mode"] == "LIVE" and config.SEND_ORDER_TAG) else None
        cur = c.execute(
            """INSERT INTO orders(order_key, bot_id, signal_key, lot_id, side, mode, symbol, instrumentId,
                   exchange, product, qty, reference_price, state, order_tag, created_at, submitted_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_key, bot["bot_id"], signal_key, lot_id, side, bot["mode"], bot["symbol"],
             str(bot["instrumentId"]), bot["exchange"], bot["product"], int(qty), ref, state, tag,
             ts, ts, ts))
        return cur.lastrowid

    # ---------- exits ----------
    def manage_exits(self, bot, ltp, now):
        exiting_bot = bot["status"] == "EXITING"
        eod = bot["product"] == "INTRADAY" and cal.at_or_after(now, config.INTRADAY_SQUAREOFF)
        lots = db.rows("SELECT * FROM trades WHERE bot_id=? AND status='OPEN' ORDER BY id", (bot["bot_id"],))
        for lot in lots:
            if lot.get("next_exit_at"):
                nxt = _parse_ts(lot["next_exit_at"])
                if nxt and now < nxt:
                    continue
            if ltp >= float(lot["target_price"]):
                reason = "TARGET"
            elif eod:
                reason = "INTRADAY_SQUAREOFF"
            elif exiting_bot:
                reason = "MANUAL_EXIT"
            else:
                continue
            self.submit_exit(bot, lot, reason, ltp, now)

    def _sellable(self, bot, qty, _refreshed=False):
        """Check the latest broker snapshot before selling LIVE shares."""
        key = (str(bot["instrumentId"]), bot["product"])
        fresh = self.snapshot_at and (self.now() - self.snapshot_at).total_seconds() <= max(120, 3 * config.RECON_SECONDS)
        if (not fresh or key not in self.snapshot) and not _refreshed:
            self.reconcile_positions(self.now())
            return self._sellable(bot, qty, _refreshed=True)
        if not fresh or key not in self.snapshot:
            if bot["product"] == "INTRADAY":
                return False, "No fresh broker position snapshot; INTRADAY sell held back"
            return True, None
        in_flight = db.scalar(
            f"SELECT COALESCE(SUM(qty - filled_qty),0) FROM orders WHERE mode='LIVE' AND side='SELL' "
            f"AND instrumentId=? AND product=? AND state IN ({','.join('?'*len(ACTIVE_ORDER_STATES))})",
            (key[0], key[1], *ACTIVE_ORDER_STATES)) or 0
        available = self.snapshot[key] - int(in_flight)
        if available < qty and not _refreshed:
            # The snapshot may predate a recent fill: refresh once before refusing.
            self.reconcile_positions(self.now())
            return self._sellable(bot, qty, _refreshed=True)
        if available < qty:
            return False, f"Broker shows {available} sellable shares, lot needs {qty}; sell held back"
        return True, None

    def submit_exit(self, bot, lot, reason, ltp, now):
        attempt = int(lot.get("exit_attempts") or 0) + 1
        order_key = f"LOT{lot['id']}|SELL|{attempt}"
        mode = lot["mode"]
        qty = int(lot["quantity"])

        if mode == "LIVE":
            block = self.live_block_reason(bot, 0)
            if block:
                self._bot_error(bot["bot_id"], f"Exit waiting: {block}")
                return
            ok, why = self._sellable(bot, qty)
            if not ok:
                self._defer_exit(lot["id"], why, now, bump=False)
                self._bot_error(bot["bot_id"], why)
                elog("EXIT_HELD", why, level="WARNING", bot_id=bot["bot_id"], lot=lot["id"], store=False)
                return

        with db.tx() as c:
            cur = c.execute("SELECT status FROM trades WHERE id=?", (lot["id"],)).fetchone()
            if not cur or cur["status"] != "OPEN":
                return
            exists = c.execute("SELECT id FROM orders WHERE order_key=?", (order_key,)).fetchone()
            if exists:
                return
            order_id = self._insert_order(c, order_key=order_key, bot=bot, side="SELL", qty=qty, ref=ltp,
                                          signal_key=f"EXIT:{reason}", lot_id=lot["id"],
                                          state="FILLED" if mode == "PAPER" else "SUBMITTING")
            c.execute("UPDATE trades SET status='EXIT_SUBMITTED', exit_order_id=?, exit_attempts=?, "
                      "exit_reason=? WHERE id=?", (order_id, attempt, reason, lot["id"]))
            if mode == "PAPER":
                self._apply_fill(c, order_id, qty, ltp, [])

        elog("EXIT_SIGNAL", f"SELL {bot['symbol']} lot {lot['id']} qty {qty} reason {reason} ltp {ltp}",
             bot_id=bot["bot_id"], symbol=bot["symbol"], ltp=ltp, target=lot["target_price"], mode=mode,
             store=(mode == "LIVE"))
        if mode == "LIVE":
            self._send(order_id)

    def _defer_exit(self, lot_id, reason, now, bump=True):
        nxt = (now + timedelta(seconds=config.EXIT_RETRY_SECONDS)).isoformat()
        with db.tx() as c:
            c.execute("UPDATE trades SET next_exit_at=? WHERE id=?", (nxt, lot_id))

    # ---------- broker submission ----------
    def _send(self, order_id):
        o = db.row("SELECT * FROM orders WHERE id=?", (order_id,))
        if not o or o["state"] != "SUBMITTING":
            return
        payload = self.broker.build_order(instrument_id=o["instrumentId"], exchange=o["exchange"],
                                          side=o["side"], quantity=o["qty"], product=o["product"],
                                          tag=o["order_tag"])
        res = self.broker.place_order(payload)
        outcome = res["outcome"]
        body = json.dumps(res.get("body"), default=str)[:2000] if res.get("body") is not None else None
        ts = db.now_utc()

        with db.tx() as c:
            if outcome == "ACCEPTED":
                c.execute("UPDATE orders SET state='SUBMITTED', broker_order_id=?, response_json=?, updated_at=? "
                          "WHERE id=? AND state='SUBMITTING'", (res["broker_order_id"], body, ts, order_id))
                c.execute("UPDATE bots SET last_order=?, updated_at=? WHERE bot_id=?",
                          (f"{o['side']} {o['qty']} sent #{res['broker_order_id']}", ts, o["bot_id"]))
            elif outcome == "UNKNOWN":
                c.execute("UPDATE orders SET state='UNKNOWN', reject_reason=?, response_json=?, updated_at=? "
                          "WHERE id=? AND state='SUBMITTING'", (res.get("message") or "no clear reply", body, ts, order_id))
                c.execute("UPDATE bots SET last_error=?, updated_at=? WHERE bot_id=?",
                          (f"{o['side']} order outcome unknown; checking IIFL order book", ts, o["bot_id"]))
            else:
                reason = res.get("message") or outcome
                self._finalize_reject(c, o, "REJECTED", f"{outcome}: {reason}", body)

        if outcome == "SESSION":
            self.set_session("EXPIRED", res.get("message") or "")
        elog("ORDER_" + outcome, f"{o['side']} {o['symbol']} qty {o['qty']} -> {outcome} {res.get('message') or ''}".strip(),
             level="INFO" if outcome == "ACCEPTED" else "WARNING", bot_id=o["bot_id"],
             symbol=o["symbol"], broker_order_id=res.get("broker_order_id"), http=res.get("http_status"),
             order_key=o["order_key"])

    # ---------- order sync ----------
    def sync_live_orders(self, now):
        active = db.rows(
            "SELECT * FROM orders WHERE mode='LIVE' AND state IN ('SUBMITTED','PARTIALLY_FILLED','UNKNOWN') ORDER BY id")
        if not active:
            return
        book = self.broker.order_book()
        by_id = {r["broker_order_id"]: r for r in book if r["broker_order_id"]}
        linked = {r["broker_order_id"] for r in db.rows(
            "SELECT broker_order_id FROM orders WHERE broker_order_id IS NOT NULL")}
        trades_cache = {}

        def trade_rows(boid):
            if "rows" not in trades_cache:
                try:
                    trades_cache["rows"] = self.broker.trade_book()
                except self.broker.SessionExpired:
                    raise
                except Exception as exc:
                    log.warning("event=TRADEBOOK_FAILED error=%s", exc)
                    trades_cache["rows"] = []
            return [t for t in trades_cache["rows"] if t["broker_order_id"] == boid]

        for o in active:
            age = (now - (_parse_ts(o["submitted_at"]) or now)).total_seconds()
            if o["state"] == "UNKNOWN":
                match = self._match_unknown(o, book, linked)
                if match is None:
                    if age > config.UNKNOWN_ORDER_TIMEOUT_SECONDS:
                        with db.tx() as c:
                            self._finalize_reject(c, o, "REJECTED",
                                                  f"Not found in IIFL order book after {int(age)}s; treated as not placed")
                        elog("ORDER_NOT_FOUND", "Unknown order not in IIFL order book; treated as not placed",
                             level="WARNING", bot_id=o["bot_id"], order_key=o["order_key"])
                    continue
                with db.tx() as c:
                    c.execute("UPDATE orders SET state='SUBMITTED', broker_order_id=?, updated_at=? WHERE id=?",
                              (match["broker_order_id"], db.now_utc(), o["id"]))
                linked.add(match["broker_order_id"])
                elog("ORDER_MATCHED", f"Unknown order matched to IIFL order {match['broker_order_id']}",
                     bot_id=o["bot_id"], broker_order_id=match["broker_order_id"], order_key=o["order_key"])
                o = db.row("SELECT * FROM orders WHERE id=?", (o["id"],))

            r = by_id.get(o["broker_order_id"])
            if r is None:
                if age > 900:
                    self._bot_error(o["bot_id"], f"Order #{o['broker_order_id']} missing from IIFL order book")
                continue
            self._apply_book_row(o, r, trade_rows, now, age)

    def _match_unknown(self, o, book, linked):
        if o["order_tag"]:
            for r in book:
                if r["tag"] and r["tag"] == o["order_tag"]:
                    return r
        candidates = [
            r for r in book
            if r["broker_order_id"] and r["broker_order_id"] not in linked
            and r["instrument_id"] == str(o["instrumentId"]) and r["side"] == o["side"]
            and r["quantity"] == int(o["qty"]) and (not r["product"] or r["product"] == o["product"])
        ]
        return candidates[-1] if len(candidates) >= 1 else None

    def _apply_book_row(self, o, r, trade_rows, now, age):
        status = r["status"]
        filled = r["filled_qty"]
        if status == "COMPLETE" and filled <= 0:
            filled = int(o["qty"])

        terminal_with_fill = status == "COMPLETE" or (status in ("CANCELLED", "REJECTED") and filled > 0)
        if terminal_with_fill:
            fills = trade_rows(o["broker_order_id"])
            fill_qty = sum(f["qty"] for f in fills)
            if fills and fill_qty == filled and all(f["price"] > 0 for f in fills):
                price = sum(f["qty"] * f["price"] for f in fills) / fill_qty
            else:
                price = r["avg_price"]
                fills = []
            if not price:
                if age > 600:
                    self._bot_error(o["bot_id"], f"Order #{o['broker_order_id']} filled but no fill price yet")
                return
            with db.tx() as c:
                c.execute("UPDATE orders SET broker_status=? WHERE id=?", (r["raw_status"], o["id"]))
                self._apply_fill(c, o["id"], filled, price, fills)
            return

        if status in ("REJECTED", "CANCELLED"):
            with db.tx() as c:
                self._finalize_reject(c, o, status, r["reject_reason"] or r["raw_status"])
            elog("ORDER_" + status, f"{o['side']} {o['symbol']} #{o['broker_order_id']} {r['reject_reason']}",
                 level="WARNING", bot_id=o["bot_id"], broker_order_id=o["broker_order_id"])
            return

        new_state = "PARTIALLY_FILLED" if filled > 0 else "SUBMITTED"
        with db.tx() as c:
            c.execute("UPDATE orders SET state=?, filled_qty=?, avg_price=?, broker_status=?, updated_at=? WHERE id=?",
                      (new_state, filled, r["avg_price"], r["raw_status"], db.now_utc(), o["id"]))

    # ---------- state transitions (call inside a transaction) ----------
    def _apply_fill(self, c, order_id, qty, price, fills):
        o = dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
        if o["state"] == "FILLED" and o["avg_price"] is not None:
            return
        ts = db.now_utc()
        price = float(price)
        qty = int(qty)
        for f in fills:
            c.execute("INSERT OR IGNORE INTO fills(order_id, broker_order_id, trade_ref, qty, price, fill_time, created_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (order_id, o["broker_order_id"], f["trade_ref"] or f"{o['broker_order_id']}-{f['qty']}-{f['price']}",
                       f["qty"], f["price"], f.get("time"), ts))
        if not fills:
            c.execute("INSERT OR IGNORE INTO fills(order_id, broker_order_id, trade_ref, qty, price, fill_time, created_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (order_id, o["broker_order_id"], "avg", qty, price, None, ts))
        c.execute("UPDATE orders SET state='FILLED', filled_qty=?, avg_price=?, completed_at=?, updated_at=? WHERE id=?",
                  (qty, price, ts, ts, order_id))

        bot = dict(c.execute("SELECT * FROM bots WHERE bot_id=?", (o["bot_id"],)).fetchone())
        if o["side"] == "BUY":
            target = target_from_fill(price, bot["target"], bot["tick_size"])
            cur = c.execute(
                """INSERT INTO trades(bot_id, symbol, side, quantity, entry_price, target_price, mode, product,
                       timeframe, status, entry_order_id, broker_buy_order_id, pnl, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (o["bot_id"], o["symbol"], "BUY", qty, price, target, o["mode"], o["product"], bot["timeframe"],
                 "OPEN", order_id, o["broker_order_id"], 0.0, ts))
            lot_id = cur.lastrowid
            c.execute("UPDATE orders SET lot_id=? WHERE id=?", (lot_id, order_id))
            c.execute("UPDATE bots SET last_order=?, last_error=NULL, updated_at=? WHERE bot_id=?",
                      (f"BUY {qty} filled @ {price:.2f} target {target:.2f}", ts, o["bot_id"]))
            if o["mode"] == "LIVE":
                db.add_event("FILLED", f"BUY {o['symbol']} {qty} @ {price:.2f}, target {target:.2f}",
                             bot_id=o["bot_id"], data={"lot": lot_id, "broker_order_id": o["broker_order_id"]})
            log.info("event=BUY_FILLED bot_id=%s symbol=%s qty=%s fill=%.2f target=%.2f broker_order_id=%s",
                     o["bot_id"], o["symbol"], qty, price, target, o["broker_order_id"])
        else:
            lot = dict(c.execute("SELECT * FROM trades WHERE id=?", (o["lot_id"],)).fetchone())
            lot_qty = int(lot["quantity"])
            sold = min(qty, lot_qty)
            pnl = _money((price - float(lot["entry_price"])) * sold)
            c.execute("UPDATE trades SET quantity=?, exit_price=?, pnl=?, net_pnl=NULL, status='CLOSED', "
                      "broker_sell_order_id=?, exit_order_id=?, closed_at=? WHERE id=?",
                      (sold, price, pnl, o["broker_order_id"], order_id, ts, lot["id"]))
            if sold < lot_qty:
                c.execute(
                    """INSERT INTO trades(bot_id, symbol, side, quantity, entry_price, target_price, mode, product,
                           timeframe, status, entry_order_id, broker_buy_order_id, pnl, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'OPEN',?,?,0,?)""",
                    (lot["bot_id"], lot["symbol"], lot["side"], lot_qty - sold, lot["entry_price"],
                     lot["target_price"], lot["mode"], lot["product"], lot["timeframe"],
                     lot["entry_order_id"], lot["broker_buy_order_id"], lot["created_at"]))
            c.execute("UPDATE bots SET last_order=?, updated_at=? WHERE bot_id=?",
                      (f"SELL {sold} filled @ {price:.2f} P&L {pnl:+.2f}", ts, o["bot_id"]))
            if o["mode"] == "LIVE":
                db.add_event("FILLED", f"SELL {o['symbol']} {sold} @ {price:.2f}, P&L {pnl:+.2f}",
                             bot_id=o["bot_id"], data={"lot": lot["id"], "broker_order_id": o["broker_order_id"]})
            log.info("event=SELL_FILLED bot_id=%s symbol=%s qty=%s fill=%.2f pnl=%.2f broker_order_id=%s",
                     o["bot_id"], o["symbol"], sold, price, pnl, o["broker_order_id"])

    def _finalize_reject(self, c, o, state, reason, body=None):
        ts = db.now_utc()
        c.execute("UPDATE orders SET state=?, reject_reason=?, response_json=COALESCE(?, response_json), "
                  "completed_at=?, updated_at=? WHERE id=?",
                  (state, (reason or "")[:500], body, ts, ts, o["id"]))
        if o["side"] == "SELL" and o["lot_id"]:
            lot = c.execute("SELECT exit_attempts FROM trades WHERE id=?", (o["lot_id"],)).fetchone()
            attempts = int(lot["exit_attempts"] or 0) if lot else 0
            delay = config.EXIT_RETRY_SECONDS if attempts < config.EXIT_MAX_ATTEMPTS else 1800
            nxt = (self.now() + timedelta(seconds=delay)).isoformat()
            c.execute("UPDATE trades SET status='OPEN', next_exit_at=? WHERE id=? AND status='EXIT_SUBMITTED'",
                      (nxt, o["lot_id"]))
        c.execute("UPDATE bots SET last_error=?, updated_at=? WHERE bot_id=?",
                  (f"{o['side']} {state.lower()}: {reason}"[:500], ts, o["bot_id"]))

    # ---------- positions reconciliation ----------
    def reconcile_positions(self, now):
        groups = db.rows(
            """SELECT b.instrumentId AS iid, b.tradingSymbol AS tsym, t.product AS product,
                      SUM(t.quantity) AS local_qty
               FROM trades t JOIN bots b ON b.bot_id = t.bot_id
               WHERE t.mode='LIVE' AND t.status IN ('OPEN','EXIT_SUBMITTED')
               GROUP BY b.instrumentId, t.product""")
        for b in db.rows("SELECT DISTINCT instrumentId AS iid, tradingSymbol AS tsym, product FROM bots "
                         "WHERE deleted=0 AND mode='LIVE' AND status IN ('RUNNING','EXITING')"):
            if not any(g["iid"] == b["iid"] and g["product"] == b["product"] for g in groups):
                groups.append({**b, "local_qty": 0})
        self.last_recon = now
        if not groups:
            return
        try:
            pos = self.broker.positions()
            hold = self.broker.holdings() if any(g["product"] == "DELIVERY" for g in groups) else []
        except self.broker.SessionExpired:
            raise
        except Exception as exc:
            log.warning("event=RECON_FAILED error=%s", exc)
            return

        report = []
        for g in groups:
            key = (str(g["iid"]), g["product"])
            bq = self.broker.broker_quantity(g["iid"], g["tsym"], g["product"], pos, hold)
            self.snapshot[key] = bq
            mkey = f"{g['iid']}|{g['product']}"
            short = bq < int(g["local_qty"])
            self.mismatch_counts[mkey] = self.mismatch_counts.get(mkey, 0) + 1 if short else 0
            if self.mismatch_counts[mkey] == 2:
                elog("POSITION_MISMATCH",
                     f"{g['tsym']} {g['product']}: broker {bq} < bot lots {g['local_qty']}; new entries paused",
                     level="ERROR", symbol=g["tsym"])
            report.append({"symbol": g["tsym"], "product": g["product"], "broker_qty": bq,
                           "bot_qty": int(g["local_qty"]), "mismatch": self.mismatch_counts[mkey] >= 2})
        self.snapshot_at = now
        db.set_runtime("reconciliation", {"at": now.isoformat(), "rows": report})
