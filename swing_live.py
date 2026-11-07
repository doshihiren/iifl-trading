"""Swing Scanner auto-trader: places the scanner's buys and sells by itself.

Daily cycle (no clicks needed once started):
  Evening, after the 17:30 daily-candle update (`make_plan`, worker's swing thread):
    * every open swing position whose RSI closed above the exit level, or that has
      been held the maximum number of days, is marked to SELL at the next open;
    * the scanner's BUY signals fill the free slots (lowest RSI first) and are saved
      as tomorrow's plan.
  Next trading morning from 09:15 (`execute`, inside the trading loop):
    * the marked positions are sold (engine.manage_exits, same order path as every bot);
    * the planned stocks are bought with a market order, quantity = capital ÷ max stocks
      ÷ live price, checked against swing capital and the global capital limit.
  Intraday: an optional stop loss is checked against the live price.

Every swing stock gets its own bot row (bot_id "SW-<SYMBOL>", strategy 'SWING') so
order sync, actual fills, restart recovery, manual-exit detection and reports all use
the existing engine. Buy keys are "SWING|<symbol>|<day>|BUY", so a restart never buys twice.
STOP only stops new buys; open positions keep their exits.
"""

import logging
import math
from datetime import date, datetime, timedelta

import candles as candle_util
import config
import db
import market_calendar as cal
import swing

log = logging.getLogger("iifl.swing_live")
IST = candle_util.IST

CFG_KEY = "swing_bot"
PLAN_KEY = "swing_plan"
ENTRY_START = "09:15"
ENTRY_DEADLINE = "09:45"     # buys not placed by then are skipped (the backtest assumes the open price)
DEFAULT_CFG = {"status": "STOPPED", "mode": "LIVE", "capital": 100000.0, "max_positions": 5, "params": {}}


# ---------------------------------------------------------------- config
def get_cfg():
    cfg = {**DEFAULT_CFG, **(db.get_runtime(CFG_KEY) or {})}
    cfg["params"] = {**swing.DEFAULTS, **(cfg.get("params") or {}),
                     "capital": cfg["capital"], "max_positions": cfg["max_positions"]}
    return cfg


def save_cfg(**kw):
    cfg = {**DEFAULT_CFG, **(db.get_runtime(CFG_KEY) or {})}
    cfg.update({k: v for k, v in kw.items() if v is not None})
    db.set_runtime(CFG_KEY, cfg)
    return get_cfg()


def get_plan():
    return db.get_runtime(PLAN_KEY) or {}


def next_trading_day(d):
    d = d + timedelta(days=1)
    while not cal.is_trading_day(datetime(d.year, d.month, d.day, 12, tzinfo=IST)):
        d += timedelta(days=1)
    return d


def _ist_day(ts):
    try:
        return datetime.fromisoformat(str(ts)).astimezone(IST).date().isoformat()
    except ValueError:
        return str(ts)[:10]


def swing_lots(statuses=("OPEN", "EXIT_SUBMITTED")):
    """Open swing lots; `bought_at` is when the BUY order was sent (the trading day the lot started)."""
    marks = ",".join("?" * len(statuses))
    return db.rows(f"SELECT t.*, b.instrumentId, b.tick_size, COALESCE(o.submitted_at, t.created_at) AS bought_at "
                   f"FROM trades t JOIN bots b ON b.bot_id=t.bot_id LEFT JOIN orders o ON o.id=t.entry_order_id "
                   f"WHERE b.strategy='SWING' AND t.status IN ({marks}) ORDER BY t.id", statuses)


# ---------------------------------------------------------------- evening plan
def make_plan(now, force=False):
    """Build the plan for the next trading day once the day's daily candles are in."""
    cfg = get_cfg()
    p = swing.params(cfg["params"])
    open_lots = swing_lots(("OPEN",))
    if cfg["status"] != "RUNNING" and not open_lots:
        return None
    data_day = db.scalar("SELECT MAX(d) FROM daily_candles")
    if not data_day:
        return None
    last_complete = candle_util.last_complete_day(now).isoformat()
    if data_day != last_complete:
        return None                       # today's candles not downloaded yet
    st = swing.get_state()
    if st.get("state") in ("RUNNING", "QUEUED"):
        return None                       # wait for the download to finish
    day = next_trading_day(date.fromisoformat(data_day)).isoformat()
    plan = get_plan()
    if plan.get("day") == day and plan.get("based_on") == data_day and not force:
        return None
    if (force and plan.get("day") == day and now.astimezone(IST).date().isoformat() == day
            and now.time() >= cal.parse_hhmm("09:00")):
        return None                       # today's plan is already being executed; never rebuild it mid-session

    stocks = swing.load_stocks(p, d_from=data_day, d_to=data_day)
    by_iid = {s.iid: s for s in stocks}

    exits = []
    for lot in open_lots:
        s = by_iid.get(str(lot["instrumentId"]))
        i = s.pos.get(data_day) if s else None
        if i is None:
            continue                      # no candle for that stock today (suspended?) - check again tomorrow
        r = s.rsi[i]
        entry_day = _ist_day(lot.get("bought_at") or lot["created_at"])
        held = sum(1 for d in s.days if entry_day <= d <= data_day)
        reason = None
        if not math.isnan(r) and r > p["exit_rsi"]:
            reason = f"RSI {r:.1f} above {p['exit_rsi']:g}"
        elif p["max_hold"] and held >= p["max_hold"]:
            reason = f"{held} days held"
        if reason and not lot.get("exit_signal"):
            with db.tx() as c:
                c.execute("UPDATE trades SET exit_signal=?, next_exit_at=NULL WHERE id=? AND status='OPEN'",
                          (reason, lot["id"]))
        if reason or lot.get("exit_signal"):
            exits.append({"lot": lot["id"], "symbol": lot["symbol"], "qty": lot["quantity"],
                          "reason": lot.get("exit_signal") or reason})

    entries, extra = [], []
    if cfg["status"] == "RUNNING":
        res = swing.scan(stocks, p)
        held_iids = {str(l["instrumentId"]) for l in swing_lots()}
        leaving = {x["lot"] for x in exits}
        staying = sum(1 for l in open_lots if l["id"] not in leaving)
        slots = max(0, int(cfg["max_positions"]) - staying)
        for row in res["buy"]:
            if row["instrumentId"] in held_iids:
                continue
            target = entries if len(entries) < slots else extra
            target.append({"symbol": row["symbol"], "instrumentId": row["instrumentId"], "rsi": row["rsi"],
                           "close": row["close"], "status": "PLANNED"})
    tick = {u["instrumentId"]: u["tick_size"] for u in db.rows("SELECT instrumentId, tick_size FROM swing_universe")}
    for e in entries:
        e["tick_size"] = tick.get(e["instrumentId"]) or 0.05
    plan = {"day": day, "based_on": data_day, "created_at": db.now_utc(), "entries": entries,
            "extra": extra[:10], "exits": exits, "mode": cfg["mode"]}
    db.set_runtime(PLAN_KEY, plan)
    db.add_event("SWING_PLAN", f"Swing plan for {day}: buy {', '.join(e['symbol'] for e in entries) or 'nothing'}; "
                 f"sell {', '.join(x['symbol'] for x in exits) or 'nothing'} (based on {data_day} close)")
    log.info("event=SWING_PLAN day=%s entries=%s exits=%s", day, [e["symbol"] for e in entries],
             [x["symbol"] for x in exits])
    return plan


# ---------------------------------------------------------------- morning execution
def entries_due(now):
    plan = get_plan()
    if plan.get("day") != now.astimezone(IST).date().isoformat():
        return False
    if not any(e.get("status") == "PLANNED" for e in plan.get("entries", [])):
        return False
    return cal.parse_hhmm(ENTRY_START) <= now.time() and get_cfg()["status"] == "RUNNING"


def ensure_bot(e, mode, capital):
    bot_id = f"SW-{e['symbol']}"[:40]
    ts = db.now_utc()
    with db.tx() as c:
        c.execute(
            """INSERT INTO bots(bot_id, symbol, tradingSymbol, instrumentId, exchange, tick_size, mode, qty,
                   qty_random_pct, target, timeframe, product, maxpos, capital, status, strategy, created_at, updated_at)
               VALUES(?,?,?,?,'NSEEQ',?,?,0,0,0,'1d','DELIVERY',1,?,'RUNNING','SWING',?,?)
               ON CONFLICT(bot_id) DO UPDATE SET mode=excluded.mode, capital=excluded.capital, status='RUNNING',
                   deleted=0, strategy='SWING', updated_at=excluded.updated_at""",
            (bot_id, e["symbol"], f"{e['symbol']}-EQ", e["instrumentId"], float(e.get("tick_size") or 0.05),
             mode, float(capital), ts, ts))
    return dict(db.row("SELECT * FROM bots WHERE bot_id=?", (bot_id,)))


def swing_deployed(mode):
    """Capital in swing positions that are staying (not marked to sell) plus pending swing buys."""
    held = db.scalar(
        "SELECT COALESCE(SUM(t.entry_price*t.quantity),0) FROM trades t JOIN bots b ON b.bot_id=t.bot_id "
        "WHERE b.strategy='SWING' AND t.mode=? AND t.status IN ('OPEN','EXIT_SUBMITTED') AND t.exit_signal IS NULL",
        (mode,)) or 0.0
    pending = db.scalar(
        "SELECT COALESCE(SUM(o.reference_price*o.qty),0) FROM orders o JOIN bots b ON b.bot_id=o.bot_id "
        "WHERE b.strategy='SWING' AND o.mode=? AND o.side='BUY' "
        "AND o.state IN ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED','UNKNOWN')", (mode,)) or 0.0
    return float(held) + float(pending)


def _set_entry(day, symbol, status, detail=None):
    plan = get_plan()
    if plan.get("day") != day:
        return
    for e in plan.get("entries", []):
        if e["symbol"] == symbol:
            e["status"], e["detail"] = status, detail
    db.set_runtime(PLAN_KEY, plan)


def execute(engine, now):
    """Place today's planned buys (called from the trading loop while the market is open)."""
    if not entries_due(now):
        return
    cfg = get_cfg()
    plan = get_plan()
    day = plan["day"]
    mode = cfg["mode"]
    todo = [e for e in plan["entries"] if e.get("status") == "PLANNED"]
    if cal.at_or_after(now, ENTRY_DEADLINE):
        for e in todo:
            _set_entry(day, e["symbol"], "SKIPPED", f"Not bought before {ENTRY_DEADLINE} (worker or IIFL login was not ready)")
        db.add_event("SWING_SKIPPED", f"Swing buys not placed before {ENTRY_DEADLINE}: "
                     f"{', '.join(e['symbol'] for e in todo)}", level="WARNING")
        return
    if mode == "LIVE":
        block = engine.live_block_reason({"mode": "LIVE"}, 0)
        if block:
            for e in todo:
                _set_entry(day, e["symbol"], "PLANNED", f"Waiting: {block}")
            return
    quotes = engine.quotes(sorted({(e["instrumentId"], "NSEEQ") for e in todo}))
    slot_budget = float(cfg["capital"]) / max(1, int(cfg["max_positions"]))
    for e in todo:
        q = quotes.get(e["instrumentId"]) or (None, None, None)
        ltp, qtime = q[0], q[1]
        if ltp is None:
            _set_entry(day, e["symbol"], "PLANNED", f"Waiting for a price: {q[2] or 'no quote'}")
            continue
        qdt = cal.parse_quote_time(qtime)
        if qdt is not None and qdt.astimezone(IST).date().isoformat() != day:
            _set_entry(day, e["symbol"], "PLANNED", "Waiting for today's first trade")
            continue
        bot = ensure_bot(e, mode, cfg["capital"])
        budget = min(slot_budget, float(cfg["capital"]) - swing_deployed(mode))
        qty = int(budget // ltp) if ltp > 0 else 0
        skip = None
        if qty < 1:
            skip = f"Price ₹{ltp:.2f} is above the free swing budget ₹{max(budget, 0):,.0f}"
        elif engine.deployed_capital(mode) + qty * ltp > config.GLOBAL_MAX_CAPITAL:
            skip = "Global capital limit reached"
        elif db.scalar("SELECT COUNT(*) FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')",
                       (bot["bot_id"],)):
            skip = "Already holding this stock"
        signal_key = f"SWING|{e['symbol']}|{day}|BUY"
        ts = db.now_utc()
        with db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO strategy_signals(signal_key, bot_id, slot, side, ltp, decision, detail, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (signal_key, bot["bot_id"], day, "BUY", ltp, "SKIPPED" if skip else "ORDERED", skip, ts))
            if cur.rowcount == 0:
                order_id = None          # already handled before a restart - never buy twice
            elif skip:
                order_id = None
            else:
                order_id = engine._insert_order(c, order_key=signal_key, bot=bot, side="BUY", qty=qty, ref=ltp,
                                                signal_key=signal_key, lot_id=None,
                                                state="FILLED" if mode == "PAPER" else "SUBMITTING")
                if mode == "PAPER":
                    engine._apply_fill(c, order_id, qty, ltp, [])
            c.execute("UPDATE bots SET last_entry_slot=?, last_error=?, updated_at=? WHERE bot_id=?",
                      (day, skip, ts, bot["bot_id"]))
        if cur.rowcount == 0:
            _set_entry(day, e["symbol"], "ORDERED", "Already sent earlier")
            continue
        if skip:
            _set_entry(day, e["symbol"], "SKIPPED", skip)
            db.add_event("SWING_SKIPPED", f"{e['symbol']}: {skip}", bot_id=bot["bot_id"], level="WARNING")
            continue
        _set_entry(day, e["symbol"], "ORDERED", f"BUY {qty} @~{ltp:.2f}")
        db.add_event("SWING_BUY", f"BUY {e['symbol']} qty {qty} @~{ltp:.2f} (RSI {e['rsi']})", bot_id=bot["bot_id"])
        log.info("event=SWING_BUY symbol=%s qty=%s ltp=%s rsi=%s mode=%s", e["symbol"], qty, ltp, e["rsi"], mode)
        if mode == "LIVE":
            engine._send(order_id)


def housekeeping():
    """Swing bot rows with nothing open and nothing planned go back to STOPPED (no needless quotes)."""
    plan = get_plan()
    planned = {f"SW-{e['symbol']}" for e in plan.get("entries", []) if e.get("status") == "PLANNED"}
    for b in db.rows("SELECT bot_id FROM bots WHERE strategy='SWING' AND status='RUNNING'"):
        if b["bot_id"] in planned:
            continue
        busy = db.scalar("SELECT COUNT(*) FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')",
                         (b["bot_id"],)) or db.scalar(
            "SELECT COUNT(*) FROM orders WHERE bot_id=? AND state IN ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED','UNKNOWN')",
            (b["bot_id"],))
        if not busy:
            with db.tx() as c:
                c.execute("UPDATE bots SET status='STOPPED', updated_at=? WHERE bot_id=?", (db.now_utc(), b["bot_id"]))


# ---------------------------------------------------------------- exits (called by engine.manage_exits)
_atr_cache = {}


def stop_price(lot):
    p = swing.params(get_cfg()["params"])
    entry = float(lot["entry_price"])
    if p["sl_mode"] == "pct" and p["sl_value"] > 0:
        return round(entry * (1 - p["sl_value"] / 100), 2)
    if p["sl_mode"] == "atr" and p["sl_value"] > 0:
        iid = str(lot.get("instrumentId") or db.scalar("SELECT instrumentId FROM bots WHERE bot_id=?", (lot["bot_id"],)))
        day = _ist_day(lot.get("bought_at") or lot["created_at"])
        key = (iid, day)
        if key not in _atr_cache:
            rows = db.rows("SELECT high, low, close FROM daily_candles WHERE instrumentId=? AND d<? ORDER BY d DESC LIMIT 60",
                           (iid, day))[::-1]
            a = swing.atr([r["high"] for r in rows], [r["low"] for r in rows], [r["close"] for r in rows], 14) if rows else []
            _atr_cache[key] = a[-1] if a and a[-1] is not None else None
        if _atr_cache[key]:
            return round(entry - p["sl_value"] * _atr_cache[key], 2)
    return None


def exit_reason(lot, ltp):
    if lot.get("exit_signal"):
        return lot["exit_signal"]
    sl = stop_price(lot)
    if sl and ltp <= sl:
        return f"Stop loss {sl:.2f}"
    return None


# ---------------------------------------------------------------- page data
def overview():
    cfg = get_cfg()
    lots = swing_lots()
    quotes = {b["bot_id"]: b for b in db.rows("SELECT bot_id, last_ltp, last_tick, last_error FROM bots WHERE strategy='SWING'")}
    pos = []
    for t in lots:
        b = quotes.get(t["bot_id"], {})
        ltp = b.get("last_ltp")
        pos.append({"lot": t["id"], "symbol": t["symbol"], "mode": t["mode"], "qty": t["quantity"],
                    "entry": t["entry_price"], "bought": _ist_day(t["bought_at"]), "ltp": ltp,
                    "pnl": round((ltp - t["entry_price"]) * t["quantity"], 2) if ltp else None,
                    "status": t["status"], "exit_signal": t.get("exit_signal"), "stop": stop_price(t),
                    "error": b.get("last_error")})
    closed = db.rows("SELECT t.pnl, t.mode FROM trades t JOIN bots b ON b.bot_id=t.bot_id "
                     "WHERE b.strategy='SWING' AND t.status='CLOSED' AND t.mode=?", (cfg["mode"],))
    wins = sum(1 for t in closed if (t["pnl"] or 0) > 0)
    return {"config": cfg, "plan": get_plan(), "positions": pos,
            "closed": {"trades": len(closed), "wins": wins, "pnl": round(sum(t["pnl"] or 0 for t in closed), 2)},
            "deployed": round(swing_deployed(cfg["mode"]), 2),
            "live_enabled": bool(config.LIVE_TRADING and config.IIFL_TRADING_IP_AUTHORIZED)}
