"""Bot and trade storage (SQLite-backed, same public API as the old JSON store)."""

import uuid

import db

BOT_FIELDS = {
    "symbol", "tradingSymbol", "instrumentId", "exchange", "tick_size", "mode", "qty", "qty_random_pct",
    "target", "timeframe", "product", "maxpos", "capital", "status", "last_entry_slot",
    "last_tick", "last_ltp", "last_order", "last_error", "deleted",
}


def _new_bot_id():
    return uuid.uuid4().hex[:12]


def list_bots(include_deleted=False):
    sql = "SELECT * FROM bots"
    if not include_deleted:
        sql += " WHERE deleted=0"
    return db.rows(sql + " ORDER BY created_at")


def get_bot(bot_id):
    return db.row("SELECT * FROM bots WHERE bot_id=? AND deleted=0", (bot_id,))


def add_bot(bot):
    ts = db.now_utc()
    bot_id = bot.get("bot_id") or _new_bot_id()
    with db.tx() as c:
        c.execute(
            """INSERT INTO bots(bot_id, symbol, tradingSymbol, instrumentId, exchange, tick_size,
                   mode, qty, qty_random_pct, target, timeframe, product, maxpos, capital, status,
                   created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bot_id,
                str(bot["symbol"]).upper(),
                bot.get("tradingSymbol"),
                str(bot["instrumentId"]),
                bot.get("exchange", "NSEEQ"),
                float(bot.get("tick_size") or 0.05),
                bot.get("mode", "PAPER"),
                int(bot.get("qty", 1)),
                float(bot.get("qty_random_pct", 10)),
                float(bot.get("target", 1.0)),
                bot.get("timeframe", "15m"),
                bot.get("product", "DELIVERY"),
                int(bot.get("maxpos", 10)),
                float(bot.get("capital", 100000)),
                bot.get("status", "READY"),
                ts,
                ts,
            ),
        )
    return get_bot(bot_id)


def update_bot(bot_id, **changes):
    changes = {k: v for k, v in changes.items() if k in BOT_FIELDS}
    if changes:
        changes["updated_at"] = db.now_utc()
        cols = ", ".join(f"{k}=?" for k in changes)
        with db.tx() as c:
            cur = c.execute(
                f"UPDATE bots SET {cols} WHERE bot_id=? AND deleted=0",
                (*changes.values(), bot_id),
            )
            if cur.rowcount == 0:
                return None
    return get_bot(bot_id)


def remove_bot(bot_id):
    """Soft delete so trade history keeps its bot_id."""
    with db.tx() as c:
        cur = c.execute(
            "UPDATE bots SET deleted=1, status='STOPPED', updated_at=? WHERE bot_id=? AND deleted=0",
            (db.now_utc(), bot_id),
        )
        return cur.rowcount > 0


def list_trades(limit=None):
    sql = "SELECT * FROM trades ORDER BY id"
    if limit:
        return db.rows(f"SELECT * FROM (SELECT * FROM trades ORDER BY id DESC LIMIT {int(limit)}) ORDER BY id")
    return db.rows(sql)


def open_trades(bot_id=None, symbol=None, mode=None):
    """Lots still owned by a strategy (OPEN or with an exit in flight)."""
    sql = "SELECT * FROM trades WHERE status IN ('OPEN','EXIT_SUBMITTED')"
    params = []
    if bot_id:
        sql += " AND bot_id=?"
        params.append(bot_id)
    if symbol:
        sql += " AND symbol=?"
        params.append(symbol.upper())
    if mode:
        sql += " AND mode=?"
        params.append(mode)
    return db.rows(sql + " ORDER BY id", params)


def add_trade(trade):
    ts = db.now_utc()
    with db.tx() as c:
        cur = c.execute(
            """INSERT INTO trades(bot_id, symbol, side, quantity, entry_price, target_price, mode,
                   product, timeframe, status, entry_order_id, broker_buy_order_id, pnl, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade.get("bot_id"),
                str(trade["symbol"]).upper(),
                trade.get("side", "BUY"),
                int(trade["quantity"]),
                float(trade["entry_price"]),
                float(trade["target_price"]),
                trade.get("mode", "PAPER"),
                trade.get("product"),
                trade.get("timeframe"),
                trade.get("status", "OPEN"),
                trade.get("entry_order_id"),
                trade.get("broker_buy_order_id"),
                trade.get("pnl", 0.0),
                trade.get("created_at") or ts,
            ),
        )
        trade_id = cur.lastrowid
    return db.row("SELECT * FROM trades WHERE id=?", (trade_id,))


def close_trade(trade_id, *, exit_price, broker_sell_order_id=None, exit_reason="TARGET"):
    with db.tx() as c:
        t = c.execute("SELECT * FROM trades WHERE id=?", (int(trade_id),)).fetchone()
        if not t:
            return None
        pnl = round((float(exit_price) - float(t["entry_price"])) * int(t["quantity"]), 2)
        c.execute(
            """UPDATE trades SET exit_price=?, pnl=?, broker_sell_order_id=?, status='CLOSED',
                   exit_reason=?, closed_at=? WHERE id=?""",
            (float(exit_price), pnl, broker_sell_order_id, exit_reason, db.now_utc(), int(trade_id)),
        )
    return db.row("SELECT * FROM trades WHERE id=?", (int(trade_id),))
