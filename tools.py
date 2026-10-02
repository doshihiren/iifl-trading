"""Admin commands for the VPS.

    python tools.py migrate           import /root/iifl/data/bots.json into SQLite (safe to repeat)
    python tools.py refresh-ticks     store the exchange tick size for every bot
    python tools.py status            bots, open lots and pending orders from the database
    python tools.py broker-check      read-only: session, order book, trade book, positions, holdings
    python tools.py worker-dry-run    run one engine cycle in the foreground (stop the service first)
    python tools.py candles-check SBC show IIFL's raw 1-minute candle reply for the last trading day
    python tools.py purge-paper       show what PAPER data would be deleted
    python tools.py purge-paper --yes back up the database, then delete all PAPER bots/trades/orders
"""

import json
import sys

from networking import force_ipv6

force_ipv6()

import db  # noqa: E402


def cmd_migrate():
    db.init_db()
    print(json.dumps(db.get_runtime("json_migrated"), indent=2))
    print("bots:", db.scalar("SELECT COUNT(*) FROM bots"),
          "trades:", db.scalar("SELECT COUNT(*) FROM trades"),
          "open:", db.scalar("SELECT COUNT(*) FROM trades WHERE status='OPEN'"))


def cmd_refresh_ticks():
    from app import _tick_size
    from instruments import find_instrument
    for b in db.rows("SELECT * FROM bots WHERE deleted=0"):
        try:
            inst = find_instrument(b["symbol"], exchange=b["exchange"])
        except Exception as exc:
            print(f"{b['bot_id']} {b['symbol']}: lookup failed: {exc}")
            continue
        tick = _tick_size(inst)
        with db.tx() as c:
            c.execute("UPDATE bots SET tick_size=?, tradingSymbol=COALESCE(tradingSymbol, ?) WHERE bot_id=?",
                      (tick, inst.get("tradingSymbol"), b["bot_id"]))
        print(f"{b['bot_id']} {b['symbol']}: tick_size={tick} (contract fields: "
              f"{ {k: v for k, v in inst.items() if 'tick' in k.lower() or 'lot' in k.lower()} })")


def cmd_status():
    for b in db.rows("SELECT * FROM bots WHERE deleted=0"):
        lots = db.rows("SELECT * FROM trades WHERE bot_id=? AND status IN ('OPEN','EXIT_SUBMITTED')", (b["bot_id"],))
        print(f"{b['bot_id']} {b['symbol']:<10} {b['mode']:<5} {b['timeframe']:<3} qty={b['qty']} "
              f"target={b['target']}% tick={b['tick_size']} status={b['status']} open_lots={len(lots)} "
              f"err={b['last_error'] or '-'}")
    pend = db.rows("SELECT id, bot_id, side, qty, state, broker_order_id, reject_reason FROM orders "
                   "WHERE state IN ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED','UNKNOWN')")
    print("pending orders:", pend or "none")
    print("session:", db.get_runtime("broker_session"))
    print("worker heartbeat:", db.get_runtime("worker_heartbeat"))


def _show(title, rows, limit=5):
    print(f"\n== {title}: {len(rows)} rows")
    if rows:
        keys = sorted({k for r in rows for k in r})
        print("fields:", ", ".join(keys))
        for r in rows[:limit]:
            print(json.dumps(r, default=str))


def cmd_broker_check():
    import broker
    try:
        broker.check_session()
        print("session: CONNECTED")
    except broker.SessionExpired as exc:
        print("session: EXPIRED ->", exc)
        return
    raw_orders = broker.extract_rows(broker._get("/v1/orders"))
    _show("order book (raw)", raw_orders)
    _show("order book (parsed)", [broker.normalize_order_row(r) for r in raw_orders])
    raw_trades = broker.extract_rows(broker._get("/v1/trades"))
    _show("trade book (raw)", raw_trades)
    _show("positions", broker.positions())
    _show("holdings", broker.holdings())


def cmd_worker_dry_run():
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from bot_worker import acquire_lock
    lock = acquire_lock()  # refuses to run while iifl-bot.service is running  # noqa: F841
    from engine import Engine
    e = Engine()
    e.startup()
    e.run_once()
    cmd_status()


def cmd_candles_check():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import candles
    import market_data
    from instruments import find_instrument
    symbol = sys.argv[2] if len(sys.argv) > 2 else "SBC"
    inst = find_instrument(symbol)
    day = candles.last_complete_day(datetime.now(ZoneInfo("Asia/Kolkata")))
    status, body = market_data.historical_candles(inst["instrumentId"], exchange=inst.get("exchange", "NSEEQ"),
                                                  timeframe="1m", from_date=day.strftime("%d-%b-%Y"),
                                                  to_date=day.strftime("%d-%b-%Y"))
    if status in (401, 403):
        print(f"HTTP {status}: IIFL session is not valid. Open the dashboard, click 'Login to IIFL', then run this again.")
        return
    raw = json.dumps(body, default=str)
    print(f"{symbol} {day} HTTP {status}; raw reply ({len(raw)} chars), first 1500 chars:")
    print(raw[:1500])
    parsed = candles.parse_candles(body)
    print(f"\nParsed {len(parsed)} one-minute candles.")
    if parsed:
        print("first:", parsed[0])
        print("last: ", parsed[-1])


def cmd_purge_paper():
    import os
    import sqlite3
    from datetime import datetime

    import config
    db.init_db()
    paper_bots = [r["bot_id"] for r in db.rows("SELECT bot_id FROM bots WHERE mode='PAPER'")]
    counts = {
        "bots": len(paper_bots),
        "trades": db.scalar("SELECT COUNT(*) FROM trades WHERE mode='PAPER'"),
        "orders": db.scalar("SELECT COUNT(*) FROM orders WHERE mode='PAPER'"),
        "fills": db.scalar("SELECT COUNT(*) FROM fills WHERE order_id IN (SELECT id FROM orders WHERE mode='PAPER')"),
        "signals": db.scalar(f"SELECT COUNT(*) FROM strategy_signals WHERE bot_id IN ({','.join('?'*len(paper_bots)) or 'NULL'})", paper_bots),
    }
    live = {"bots": db.scalar("SELECT COUNT(*) FROM bots WHERE mode='LIVE'"),
            "trades": db.scalar("SELECT COUNT(*) FROM trades WHERE mode='LIVE'")}
    print("PAPER data to delete:", counts)
    print("LIVE data kept:     ", live)
    if "--yes" not in sys.argv:
        print("\nNothing deleted. Run again with --yes to delete.")
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(config.DATA_DIR, f"iifl-before-paper-purge-{stamp}.db")
    src = sqlite3.connect(config.BOT_DB_FILE)
    dst = sqlite3.connect(backup)
    src.backup(dst)
    dst.close(); src.close()
    os.chmod(backup, 0o600)
    print("Backup written:", backup)

    marks = ",".join("?" * len(paper_bots)) or "NULL"
    with db.tx() as c:
        c.execute("DELETE FROM fills WHERE order_id IN (SELECT id FROM orders WHERE mode='PAPER')")
        c.execute("DELETE FROM orders WHERE mode='PAPER'")
        c.execute("DELETE FROM trades WHERE mode='PAPER'")
        c.execute(f"DELETE FROM strategy_signals WHERE bot_id IN ({marks})", paper_bots)
        c.execute(f"DELETE FROM events WHERE bot_id IN ({marks})", paper_bots)
        c.execute("DELETE FROM bots WHERE mode='PAPER'")
        db.add_event("PAPER_PURGE", f"Deleted PAPER data {counts}; backup {os.path.basename(backup)}")
    print("PAPER data deleted. LIVE now:", {"bots": db.scalar("SELECT COUNT(*) FROM bots"),
                                             "trades": db.scalar("SELECT COUNT(*) FROM trades")})


COMMANDS = {
    "migrate": cmd_migrate,
    "refresh-ticks": cmd_refresh_ticks,
    "status": cmd_status,
    "broker-check": cmd_broker_check,
    "worker-dry-run": cmd_worker_dry_run,
    "purge-paper": cmd_purge_paper,
    "candles-check": cmd_candles_check,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
