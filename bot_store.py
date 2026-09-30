import json
import os
import threading
from copy import deepcopy

from config import BOT_DATA_FILE

_LOCK = threading.Lock()


def _default():
    return {"bots": [], "trades": []}


def load_store():
    with _LOCK:
        if not os.path.exists(BOT_DATA_FILE):
            return _default()
        try:
            with open(BOT_DATA_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            return _default()
        data.setdefault("bots", [])
        data.setdefault("trades", [])
        return data


def save_store(data):
    with _LOCK:
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


def upsert_bot(bot):
    data = load_store()
    symbol = bot["symbol"].upper()
    bot["symbol"] = symbol
    bots = [b for b in data["bots"] if b.get("symbol") != symbol]
    bots.append(bot)
    data["bots"] = bots
    save_store(data)
    return deepcopy(bot)


def remove_bot(symbol):
    data = load_store()
    symbol = symbol.upper()
    before = len(data["bots"])
    data["bots"] = [b for b in data["bots"] if b.get("symbol") != symbol]
    save_store(data)
    return len(data["bots"]) != before


def list_trades():
    return deepcopy(load_store()["trades"])
