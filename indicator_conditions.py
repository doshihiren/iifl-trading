"""RSI and volume condition helper for PAPER bot filtering.

The helper is intentionally read-only: it fetches recent IIFL candles and
returns indicator values. 0/blank bot settings are treated as disabled.
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import candles as candle_store
import rsi_backtest as rb

IST = ZoneInfo("Asia/Kolkata")
_CACHE = {}
TTL_SECONDS = 55


def enabled(bot):
    return any(float(bot.get(k) or 0) > 0 for k in (
        "entry_rsi_max", "exit_rsi_min", "entry_volume_min", "exit_volume_min"
    ))


def snapshot(bot, now=None):
    now = now or datetime.now(IST)
    key = (str(bot["instrumentId"]), str(bot.get("exchange") or "NSEEQ"))
    cached = _CACHE.get(key)
    if cached and time.time() - cached["at"] < TTL_SECONDS:
        return cached["value"]

    rows = candle_store.iifl_fetch(
        key[0],
        key[1],
        now.date() - timedelta(days=5),
        now.date(),
    )
    if len(rows) < 22:
        raise RuntimeError("Not enough candle data for RSI / volume conditions")

    closes = [float(x["close"]) for x in rows]
    r = rb.rsi_series(closes, rb.DEFAULT_PERIOD)
    current_rsi = float(r[-1])
    previous_rsi = float(r[-2])

    vols = [float(x.get("volume") or 0) for x in rows]
    avg20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else 0
    current_volume = vols[-1] if vols else 0
    ratio = current_volume / avg20 if avg20 > 0 and current_volume > 0 else None

    value = {
        "rsi": round(current_rsi, 2),
        "rsi_prev": round(previous_rsi, 2),
        "volume": int(current_volume),
        "volume_ratio": round(ratio, 3) if ratio is not None else None,
        "candle": rows[-1]["ts"],
    }
    _CACHE[key] = {"at": time.time(), "value": value}
    return value


def entry_block_reason(bot, snap):
    rsi_limit = float(bot.get("entry_rsi_max") or 0)
    vol_min = float(bot.get("entry_volume_min") or 0)

    if rsi_limit > 0 and snap["rsi"] > rsi_limit:
        return f"Entry RSI condition not met: {snap['rsi']:.1f} > {rsi_limit:g}"

    if vol_min > 0:
        ratio = snap.get("volume_ratio")
        if ratio is None:
            return "Entry Volume condition not met: volume ratio unavailable"
        if ratio < vol_min:
            return f"Entry Volume condition not met: {ratio:.2f}x < {vol_min:g}x"

    return None


def exit_block_reason(bot, snap):
    rsi_min = float(bot.get("exit_rsi_min") or 0)
    vol_min = float(bot.get("exit_volume_min") or 0)

    if rsi_min > 0 and snap["rsi"] < rsi_min:
        return f"Exit RSI condition not met: {snap['rsi']:.1f} < {rsi_min:g}"

    if vol_min > 0:
        ratio = snap.get("volume_ratio")
        if ratio is None:
            return "Exit Volume condition not met: volume ratio unavailable"
        if ratio < vol_min:
            return f"Exit Volume condition not met: {ratio:.2f}x < {vol_min:g}x"

    return None
