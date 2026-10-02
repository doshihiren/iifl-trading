"""Report aggregation for the /iifl/reports page (LIVE trades only).

Realized results (closed trades) use the IST date a trade CLOSED. The trade
list shows every bot trade that was entered or closed in the range, plus every
position still open, so each entry can be followed from buy to exit.
"""

import csv
import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import db

IST = ZoneInfo("Asia/Kolkata")

GROUPS = {
    "day": "Day",
    "week": "Week",
    "month": "Month",
    "symbol": "Script",
    "bot": "Bot",
    "exit_reason": "How it exited",
    "timeframe": "Timeframe",
    "product": "Product",
}

# exit_reason -> label shown to the user
EXIT_LABELS = {
    "TARGET": "Target hit",
    "MANUAL": "Exited manually (IIFL)",
    "MANUAL_EXIT": "Exit & Stop (dashboard)",
    "INTRADAY_SQUAREOFF": "Intraday square-off",
}

STATUS_FILTERS = {
    "": "All trades",
    "open": "Open",
    "closed": "Closed (any way)",
    "TARGET": "Target hit",
    "MANUAL": "Exited manually (IIFL)",
    "MANUAL_EXIT": "Exit & Stop (dashboard)",
    "INTRADAY_SQUAREOFF": "Intraday square-off",
}


def exit_label(reason):
    return EXIT_LABELS.get(reason or "TARGET", reason or "Target hit")


def status_label(t):
    if t["status"] == "OPEN":
        return "Open"
    if t["status"] == "EXIT_SUBMITTED":
        return "Selling…"
    return exit_label(t.get("exit_reason"))


def _ist(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST)


def _parse_day(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bot_label(bot):
    if not bot:
        return "-"
    return f"{bot['symbol']} {bot['timeframe']} ({bot['bot_id'][:6]})"


def _group_key(t, group, bots):
    closed = t["_closed_ist"]
    if group == "day":
        return closed.strftime("%Y-%m-%d")
    if group == "week":
        y, w, _ = closed.isocalendar()
        monday = closed.date() - timedelta(days=closed.weekday())
        return f"{y}-W{w:02d} (from {monday:%d %b})"
    if group == "month":
        return closed.strftime("%Y-%m")
    if group == "symbol":
        return t["symbol"]
    if group == "bot":
        return _bot_label(bots.get(t["bot_id"]))
    if group == "exit_reason":
        return exit_label(t.get("exit_reason"))
    if group == "timeframe":
        return t.get("timeframe") or "-"
    if group == "product":
        return t.get("product") or "-"
    return "All"


def _stats(trades):
    pnls = [float(t["pnl"] or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    hold = [
        (t["_closed_ist"] - t["_entry_ist"]).total_seconds() / 3600.0
        for t in trades if t["_entry_ist"] and t["_closed_ist"]
    ]
    buy_value = sum(float(t["entry_price"]) * int(t["quantity"]) for t in trades)
    sell_value = sum(float(t["exit_price"] or 0) * int(t["quantity"]) for t in trades)
    fees = [float(t["fees"]) for t in trades if t.get("fees") is not None]
    gross = sum(pnls)
    return {
        "trades": len(trades),
        "qty": sum(int(t["quantity"]) for t in trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / len(trades), 1) if trades else 0.0,
        "gross_pnl": round(gross, 2),
        "fees": round(sum(fees), 2) if fees else None,
        "net_pnl": round(gross - sum(fees), 2) if fees else None,
        "avg_pnl": round(gross / len(trades), 2) if trades else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best": round(max(pnls), 2) if pnls else 0.0,
        "worst": round(min(pnls), 2) if pnls else 0.0,
        "buy_value": round(buy_value, 2),
        "turnover": round(buy_value + sell_value, 2),
        "return_pct": round(100.0 * gross / buy_value, 3) if buy_value else 0.0,
        "avg_hold_hours": round(sum(hold) / len(hold), 2) if hold else None,
    }


def build(params):
    """params: from, to (YYYY-MM-DD), symbol, bot_id, group, status."""
    today = datetime.now(IST).date()
    d_to = _parse_day(params.get("to")) or today
    d_from = _parse_day(params.get("from")) or (d_to - timedelta(days=29))
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    symbol = (params.get("symbol") or "").upper().strip()
    bot_id = (params.get("bot_id") or "").strip()
    group = params.get("group") if params.get("group") in GROUPS else "day"
    status = params.get("status") if params.get("status") in STATUS_FILTERS else ""

    bots = {b["bot_id"]: b for b in db.rows("SELECT * FROM bots WHERE mode='LIVE'")}

    def in_range(dt):
        return dt is not None and d_from <= dt.date() <= d_to

    def scope(t):
        if symbol and t["symbol"] != symbol:
            return False
        if bot_id and t["bot_id"] != bot_id:
            return False
        return True

    all_trades = []
    for t in db.rows("SELECT * FROM trades WHERE mode='LIVE' ORDER BY id"):
        t["_entry_ist"] = _ist(t["created_at"])
        t["_closed_ist"] = _ist(t["closed_at"])
        all_trades.append(t)

    closed = [t for t in all_trades if t["status"] == "CLOSED" and in_range(t["_closed_ist"]) and scope(t)]
    open_now = [t for t in all_trades if t["status"] != "CLOSED" and scope(t)]

    # Groups (realized, by close date)
    buckets = {}
    for t in closed:
        buckets.setdefault(_group_key(t, group, bots), []).append(t)
    group_rows = [{"key": k, **_stats(v)} for k, v in buckets.items()]
    if group in ("day", "week", "month"):
        group_rows.sort(key=lambda r: r["key"], reverse=True)
    else:
        group_rows.sort(key=lambda r: r["gross_pnl"], reverse=True)

    # Daily series (every calendar day in range)
    by_day = {}
    for t in closed:
        by_day.setdefault(t["_closed_ist"].strftime("%Y-%m-%d"), []).append(float(t["pnl"] or 0))
    daily, running = [], 0.0
    for i in range((d_to - d_from).days + 1):
        d = (d_from + timedelta(days=i)).isoformat()
        p = round(sum(by_day.get(d, [])), 2)
        running = round(running + p, 2)
        daily.append({"day": d, "pnl": p, "trades": len(by_day.get(d, [])), "cumulative": running})

    # How trades exited
    exits = {}
    for t in closed:
        exits.setdefault(exit_label(t.get("exit_reason")), []).append(t)
    exit_rows = sorted(({"key": k, **_stats(v)} for k, v in exits.items()), key=lambda r: -r["trades"])

    # Open positions (current)
    open_rows, unreal_total = [], 0.0
    for t in open_now:
        bot = bots.get(t["bot_id"]) or {}
        ltp = bot.get("last_ltp")
        unreal = round((float(ltp) - float(t["entry_price"])) * int(t["quantity"]), 2) if ltp else None
        if unreal is not None:
            unreal_total += unreal
        open_rows.append({"qty": t["quantity"], "entry": t["entry_price"]})

    # Entries made in range (bot activity)
    entered = [t for t in all_trades if in_range(t["_entry_ist"]) and scope(t)]

    # LIVE order outcomes in range
    order_stats = {}
    for o in db.rows("SELECT state, side, created_at, symbol, bot_id FROM orders WHERE mode='LIVE'"):
        dt = _ist(o["created_at"])
        if not in_range(dt) or (symbol and o["symbol"] != symbol) or (bot_id and o["bot_id"] != bot_id):
            continue
        key = f"{o['side']} {o['state']}"
        order_stats[key] = order_stats.get(key, 0) + 1

    # Trade list: entered or closed in range, plus everything still open
    listed = {}
    for t in all_trades:
        if not scope(t):
            continue
        if t["status"] != "CLOSED" or in_range(t["_entry_ist"]) or in_range(t["_closed_ist"]):
            listed[t["id"]] = t
    rows = list(listed.values())
    if status == "open":
        rows = [t for t in rows if t["status"] != "CLOSED"]
    elif status == "closed":
        rows = [t for t in rows if t["status"] == "CLOSED"]
    elif status:
        rows = [t for t in rows if t["status"] == "CLOSED" and (t.get("exit_reason") or "TARGET") == status]
    rows.sort(key=lambda t: (t["_closed_ist"] or t["_entry_ist"] or datetime.min.replace(tzinfo=IST)),
              reverse=True)
    trade_rows = [_trade_row(t, bots) for t in rows][:2000]

    return {
        "filters": {"from": d_from.isoformat(), "to": d_to.isoformat(), "symbol": symbol,
                    "bot_id": bot_id, "group": group, "status": status},
        "groups_available": GROUPS,
        "status_filters": STATUS_FILTERS,
        "bots": [{"bot_id": b["bot_id"], "label": _bot_label(b), "deleted": b["deleted"]} for b in bots.values()],
        "symbols": sorted({t["symbol"] for t in all_trades} | {b["symbol"] for b in bots.values()}),
        "summary": _stats(closed),
        "entries": {"count": len(entered), "qty": sum(int(t["quantity"]) for t in entered),
                    "value": round(sum(float(t["entry_price"]) * int(t["quantity"]) for t in entered), 2)},
        "open": {"count": len(open_rows), "qty": sum(int(r["qty"]) for r in open_rows),
                 "capital": round(sum(float(r["entry"]) * int(r["qty"]) for r in open_rows), 2),
                 "unrealized": round(unreal_total, 2)},
        "exits": exit_rows,
        "groups": group_rows,
        "daily": daily,
        "order_stats": order_stats,
        "trades": trade_rows,
    }


def _trade_row(t, bots):
    bot = bots.get(t["bot_id"])
    now = datetime.now(IST)
    hold = None
    if t["_entry_ist"]:
        hold = round(((t["_closed_ist"] or now) - t["_entry_ist"]).total_seconds() / 3600.0, 2)
    ltp = (bot or {}).get("last_ltp")
    unreal = None
    if t["status"] != "CLOSED" and ltp:
        unreal = round((float(ltp) - float(t["entry_price"])) * int(t["quantity"]), 2)
    return {
        "id": t["id"], "bot": _bot_label(bot) if bot else (t["bot_id"] or "-"), "bot_id": t["bot_id"],
        "symbol": t["symbol"], "product": t.get("product"), "timeframe": t.get("timeframe"),
        "qty": t["quantity"], "entry_time": t["_entry_ist"].strftime("%Y-%m-%d %H:%M:%S") if t["_entry_ist"] else "",
        "entry": t["entry_price"], "target": t["target_price"],
        "status": t["status"], "status_label": status_label(t),
        "exit_time": t["_closed_ist"].strftime("%Y-%m-%d %H:%M:%S") if t["_closed_ist"] else "",
        "exit": t["exit_price"], "gross_pnl": t["pnl"] if t["status"] == "CLOSED" else None,
        "ltp": ltp if t["status"] != "CLOSED" else None, "unrealized": unreal,
        "fees": t.get("fees"), "net_pnl": t.get("net_pnl"),
        "hold_hours": hold, "exit_reason": t.get("exit_reason") or "", "exit_note": t.get("exit_note") or "",
        "broker_buy_order_id": t.get("broker_buy_order_id") or "", "broker_sell_order_id": t.get("broker_sell_order_id") or "",
    }


CSV_COLUMNS = ["id", "symbol", "bot", "product", "timeframe", "qty", "entry_time", "entry", "target",
               "status_label", "exit_time", "exit", "gross_pnl", "ltp", "unrealized", "fees", "net_pnl",
               "hold_hours", "exit_note", "broker_buy_order_id", "broker_sell_order_id"]


def to_csv(report):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in report["trades"]:
        w.writerow(r)
    return buf.getvalue()
