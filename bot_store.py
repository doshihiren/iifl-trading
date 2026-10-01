import json
import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from config import BOT_DATA_FILE

_LOCK = threading.Lock()


def _default():
    return {"bots": [], "trades": []}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_bot_id():
    return uuid.uuid4().hex[:12]


def _normalize(data):
    changed = False
    data.setdefault("bots", [])
    data.setdefault("trades", [])

    for bot in data["bots"]:
        if not bot.get("bot_id"):
            bot["bot_id"] = _new_bot_id()
            changed = True

    # Migrate old trades when a symbol currently maps to exactly one bot.
    by_symbol = {}
    for bot in data["bots"]:
        by_symbol.setdefault(bot.get("symbol"), []).append(bot)

    for trade in data["trades"]:
        if trade.get("bot_id"):
            continue
        matches = by_symbol.get(trade.get("symbol"), [])
        if len(matches) == 1:
            trade["bot_id"] = matches[0]["bot_id"]
            changed = True

    return changed


def load_store():
    with _LOCK:
        if not os.path.exists(BOT_DATA_FILE):
            return _default()

        try:
            with open(BOT_DATA_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            return _default()

        changed = _normalize(data)
        if changed:
            directory = os.path.dirname(BOT_DATA_FILE)
            os.makedirs(directory, exist_ok=True)
            tmp = BOT_DATA_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, BOT_DATA_FILE)
            os.chmod(BOT_DATA_FILE, 0o600)

        return data


def save_store(data):
    with _LOCK:
        _normalize(data)
        directory = os.path.dirname(BOT_DATA_FILE)
        os.makedirs(directory, exist_ok=True)
        tmp = BOT_DATA_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BOT_DATA_FILE)
        os.chmod(BOT_DATA_FILE, 0o600)


def list_bots():
    return deepcopy(load_store()["bots"])


def get_bot(bot_id):
    for bot in load_store()["bots"]:
        if bot.get("bot_id") == bot_id:
            return deepcopy(bot)
    return None


def add_bot(bot):
    data = load_store()
    row = deepcopy(bot)
    row["bot_id"] = row.get("bot_id") or _new_bot_id()
    row["symbol"] = str(row["symbol"]).upper()
    data["bots"].append(row)
    save_store(data)
    return deepcopy(row)


def update_bot(bot_id, **changes):
    data = load_store()
    found = None

    for bot in data["bots"]:
        if bot.get("bot_id") == bot_id:
            bot.update(changes)
            found = deepcopy(bot)
            break

    if found is None:
        return None

    save_store(data)
    return found


def remove_bot(bot_id):
    data = load_store()
    before = len(data["bots"])
    data["bots"] = [b for b in data["bots"] if b.get("bot_id") != bot_id]
    save_store(data)
    return len(data["bots"]) != before


def list_trades():
    return deepcopy(load_store()["trades"])


def open_trades(bot_id=None, symbol=None):
    trades = [
        t for t in load_store()["trades"]
        if str(t.get("status", "")).upper() == "OPEN"
    ]

    if bot_id:
        trades = [t for t in trades if t.get("bot_id") == bot_id]

    if symbol:
        symbol = symbol.upper()
        trades = [t for t in trades if t.get("symbol") == symbol]

    return deepcopy(trades)


def add_trade(trade):
    data = load_store()
    next_id = max([int(t.get("id", 0)) for t in data["trades"]] or [0]) + 1
    row = {
        "id": next_id,
        "created_at": _now(),
        "status": "OPEN",
        **trade,
    }
    data["trades"].append(row)
    save_store(data)
    return deepcopy(row)


def close_trade(trade_id, *, exit_price, broker_sell_order_id=None):
    data = load_store()
    closed = None

    for trade in data["trades"]:
        if int(trade.get("id", 0)) == int(trade_id):
            entry = float(trade.get("entry_price", 0))
            qty = int(trade.get("quantity", trade.get("qty", 0)))
            trade["exit_price"] = float(exit_price)
            trade["pnl"] = round((float(exit_price) - entry) * qty, 2)
            trade["broker_sell_order_id"] = broker_sell_order_id
            trade["status"] = "CLOSED"
            trade["closed_at"] = _now()
            closed = deepcopy(trade)
            break

    if closed is not None:
        save_store(data)

    return closed
