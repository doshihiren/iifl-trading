"""Admin commands for the VPS.

    python tools.py migrate           import /root/iifl/data/bots.json into SQLite (safe to repeat)
    python tools.py refresh-ticks     store the exchange tick size for every bot
    python tools.py status            bots, open lots and pending orders from the database
    python tools.py broker-check      read-only: session, order book, trade book, positions, holdings
    python tools.py worker-dry-run    run one engine cycle in the foreground (stop the service first)
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


COMMANDS = {
    "migrate": cmd_migrate,
    "refresh-ticks": cmd_refresh_ticks,
    "status": cmd_status,
    "broker-check": cmd_broker_check,
    "worker-dry-run": cmd_worker_dry_run,
}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()
