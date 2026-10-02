import json
import logging
import os
import threading
import time

import requests

import config
from config import IIFL_BASE_URL

log = logging.getLogger("iifl.instruments")

# The NSE contract list is large, so it is downloaded at most every CACHE_HOURS
# and kept on disk (shared by the web app and the worker) and in memory.
CACHE_HOURS = 12
_mem = {}
_lock = threading.Lock()


class InstrumentLookupError(RuntimeError):
    pass


def _cache_path(exchange):
    return os.path.join(config.DATA_DIR, f"contracts-{exchange}.json")


def _download(exchange):
    response = requests.get(f"{IIFL_BASE_URL}/v1/contractfiles/{exchange}.json", timeout=45)
    response.raise_for_status()
    return response.json()


def get_contracts(exchange):
    with _lock:
        now = time.time()
        hit = _mem.get(exchange)
        if hit and now - hit[0] < CACHE_HOURS * 3600:
            return hit[1]
        path = _cache_path(exchange)
        try:
            if now - os.path.getmtime(path) < CACHE_HOURS * 3600:
                with open(path) as f:
                    data = json.load(f)
                _mem[exchange] = (os.path.getmtime(path), data)
                return data
        except (OSError, ValueError):
            pass
        try:
            data = _download(exchange)
        except Exception:
            # IIFL unreachable: fall back to an older cached copy if there is one.
            try:
                with open(path) as f:
                    data = json.load(f)
                log.warning("event=CONTRACTS_STALE exchange=%s using cached copy", exchange)
                _mem[exchange] = (now, data)
                return data
            except (OSError, ValueError):
                raise
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            log.warning("event=CONTRACTS_CACHE_WRITE_FAILED path=%s", path)
        _mem[exchange] = (now, data)
        return data


def find_instrument(symbol, exchange="NSEEQ"):
    symbol = symbol.strip().upper()
    contracts = get_contracts(exchange)

    for item in contracts:
        trading_symbol = str(item.get("tradingSymbol", "")).upper()
        underlying_symbol = str(item.get("underlyingInstrumentSymbol", "")).upper()
        underlying_name = str(item.get("underlyingInstrumentName", "")).upper()

        if symbol in {trading_symbol, underlying_symbol, underlying_name}:
            return item

        if trading_symbol == f"{symbol}-EQ":
            return item

    raise InstrumentLookupError(f"Instrument not found: {symbol} on {exchange}")
