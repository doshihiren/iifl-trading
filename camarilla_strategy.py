"""Camarilla S2/S4/R2/R4 + RSI(21) 40/60 strategy (analysis only – never places orders).

Rules (from the Josh Talks video, Nifty 15-minute chart):
  * Price comes from Camarilla pivots of the PREVIOUS day's high/low/close.
    Only S2, S4 (buy zones) and R2, R4 (sell zones) are used.
  * Direction comes from RSI length 21 with bands 60 / 40.
  * BUY  when RSI was below 40 and closes back above 40 AND the candle is at S2 or S4.
         Stop = low of the signal candle. Target = next line up (S4 -> S2, S2 -> R2).
  * SELL when RSI was above 60 and closes back below 60 AND the candle is at R2 or R4.
         Stop = high of the signal candle. Target = next line down (R4 -> R2, R2 -> S2).
  * No trade at the wrong level (buy at resistance / sell at support), when price is
    "in the air" (no level touched) or when the stop is too wide.
  * Entry at the signal candle's close; stops/targets checked on every 1-minute candle
    after that; anything still open is closed at the square-off time.

Signals are scale-free (levels and stop width are in %), so the index, a Nifty ETF
such as NIFTYBEES, or near-month futures all give the same pattern of signals.
"""

from collections import OrderedDict

DEFAULTS = {
    "timeframe_min": 15,
    "rsi_period": 21,
    "upper_band": 60.0,
    "lower_band": 40.0,
    "level_tol_pct": 0.05,     # candle is "at" a level if its range comes within this % of it
    "touch_lookback": 2,       # the level may be touched up to N candles before the RSI cross
    "max_sl_pct": 0.25,        # skip if stop is wider than this % of price (~65 pts at 26,000)
    "no_entry_after": "15:00", # signal candle must close by this time
    "square_off": "15:15",
    "charges_pts": 0.0,        # optional round-trip cost in index points per trade
}

SESSION_START_MIN = 9 * 60 + 15
LEVEL_ORDER = ("S4", "S2", "R2", "R4")       # low -> high
NEXT_UP = {"S4": "S2", "S2": "R2"}
NEXT_DOWN = {"R4": "R2", "R2": "S2"}


def _mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(total):
    return f"{total // 60:02d}:{total % 60:02d}"


def camarilla(high, low, close):
    r = high - low
    return {
        "R4": close + r * 1.1 / 2, "R3": close + r * 1.1 / 4, "R2": close + r * 1.1 / 6, "R1": close + r * 1.1 / 12,
        "S1": close - r * 1.1 / 12, "S2": close - r * 1.1 / 6, "S3": close - r * 1.1 / 4, "S4": close - r * 1.1 / 2,
    }


def rsi_wilder(closes, period):
    """Wilder RSI, same as TradingView ta.rsi. None until `period` changes are available."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gain += max(ch, 0.0)
        loss += max(-ch, 0.0)
    ag, al = gain / period, loss / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def resample(candles, tf_min):
    """1-minute candles ({ts:'YYYY-MM-DD HH:MM', open, high, low, close}) -> tf candles aligned to 09:15.
    Each bar keeps `start` (ts of first minute), `end_min` (close time in minutes) and index range into 1m list."""
    bars = []
    cur, key = None, None
    for i, c in enumerate(candles):
        day, t = c["ts"][:10], _mins(c["ts"][11:16])
        slot = (t - SESSION_START_MIN) // tf_min
        k = (day, slot)
        if k != key:
            if cur:
                bars.append(cur)
            key = k
            start = SESSION_START_MIN + slot * tf_min
            cur = {"ts": f"{day} {_hhmm(start)}", "day": day, "end_min": start + tf_min,
                   "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "i0": i, "i1": i}
        else:
            cur["high"] = max(cur["high"], c["high"])
            cur["low"] = min(cur["low"], c["low"])
            cur["close"] = c["close"]
            cur["i1"] = i
    if cur:
        bars.append(cur)
    return bars


def daily_levels(candles, prev_day=None):
    """IST date -> Camarilla levels from the previous trading day in the data.
    `prev_day` = {"high","low","close"} supplies levels for the first day."""
    days = OrderedDict()
    for c in candles:
        d = days.setdefault(c["ts"][:10], {"high": c["high"], "low": c["low"], "close": c["close"]})
        d["high"] = max(d["high"], c["high"])
        d["low"] = min(d["low"], c["low"])
        d["close"] = c["close"]
    keys = list(days)
    levels = {}
    if prev_day and keys:
        levels[keys[0]] = camarilla(prev_day["high"], prev_day["low"], prev_day["close"])
    for a, b in zip(keys, keys[1:]):
        p = days[a]
        levels[b] = camarilla(p["high"], p["low"], p["close"])
    return levels, days


def _touched(bars, i, level, p):
    band = level * p["level_tol_pct"] / 100
    for k in range(max(0, i - p["touch_lookback"]), i + 1):
        if bars[k]["day"] != bars[i]["day"]:
            continue
        if bars[k]["high"] >= level - band and bars[k]["low"] <= level + band:
            return True
    return False


def signals(candles, params=None, prev_day=None):
    """All RSI crosses with the decision taken on each. Returns (bars, rsi, levels, signal list)."""
    p = {**DEFAULTS, **(params or {})}
    bars = resample(candles, p["timeframe_min"])
    rsi = rsi_wilder([b["close"] for b in bars], p["rsi_period"])
    levels, _ = daily_levels(candles, prev_day)
    cutoff = _mins(p["no_entry_after"])
    out = []
    for i in range(1, len(bars)):
        if rsi[i] is None or rsi[i - 1] is None:
            continue
        up = rsi[i - 1] < p["lower_band"] <= rsi[i]
        down = rsi[i - 1] > p["upper_band"] >= rsi[i]
        if not (up or down):
            continue
        b = bars[i]
        lv = levels.get(b["day"])
        sig = {"bar": i, "ts": b["ts"], "close_time": _hhmm(b["end_min"]), "side": "BUY" if up else "SELL",
               "rsi": round(rsi[i], 2), "entry": b["close"], "level": None, "sl": None, "target": None,
               "taken": False, "reason": ""}
        out.append(sig)
        if not lv:
            sig["reason"] = "no previous day for pivots"
            continue
        if b["end_min"] > cutoff:
            sig["reason"] = f"after {p['no_entry_after']}"
            continue
        if up:
            level = next((k for k in ("S4", "S2") if _touched(bars, i, lv[k], p)), None)
            if not level:
                sig["reason"] = "at resistance" if any(_touched(bars, i, lv[k], p) for k in ("R2", "R4")) \
                    else "in the air (no level)"
                continue
            sl, target = b["low"], lv[NEXT_UP[level]]
            risk, beyond = b["close"] - sl, b["close"] >= target
        else:
            level = next((k for k in ("R4", "R2") if _touched(bars, i, lv[k], p)), None)
            if not level:
                sig["reason"] = "at support" if any(_touched(bars, i, lv[k], p) for k in ("S2", "S4")) \
                    else "in the air (no level)"
                continue
            sl, target = b["high"], lv[NEXT_DOWN[level]]
            risk, beyond = sl - b["close"], b["close"] <= target
        sig.update(level=level, sl=round(sl, 2), target=round(target, 2))
        if risk > b["close"] * p["max_sl_pct"] / 100:
            sig.update(reason="stop too wide", sl_points=round(risk, 1))
        elif beyond:
            sig["reason"] = "already past target"
        else:
            sig.update(taken=True, reason="TRADE")
    return bars, rsi, levels, out


def backtest(candles, params=None, trade_from=None, prev_day=None):
    """Index-points backtest, one trade at a time, exits checked minute by minute."""
    p = {**DEFAULTS, **(params or {})}
    bars, rsi, levels, sigs = signals(candles, p, prev_day)
    if trade_from:
        sigs = [s for s in sigs if s["ts"][:10] >= trade_from]
    sq = _mins(p["square_off"])
    trades, busy_until = [], -1
    for s in sigs:
        if not s["taken"]:
            continue
        b = bars[s["bar"]]
        if b["i1"] <= busy_until:
            s.update(taken=False, reason="already in a trade")
            continue
        exit_px, exit_ts, why, j = None, None, None, b["i1"]
        for j in range(b["i1"] + 1, len(candles)):
            c = candles[j]
            if c["ts"][:10] != b["day"]:
                prev = candles[j - 1]
                exit_px, exit_ts, why, j = prev["close"], prev["ts"], "EOD", j - 1
                break
            if _mins(c["ts"][11:16]) >= sq:
                exit_px, exit_ts, why = c["open"], c["ts"], "SQUARE-OFF"
                break
            if s["side"] == "BUY":
                if c["low"] <= s["sl"]:
                    exit_px, exit_ts, why = min(s["sl"], c["open"]), c["ts"], "STOP"
                    break
                if c["high"] >= s["target"]:
                    exit_px, exit_ts, why = max(s["target"], c["open"]), c["ts"], "TARGET"
                    break
            else:
                if c["high"] >= s["sl"]:
                    exit_px, exit_ts, why = max(s["sl"], c["open"]), c["ts"], "STOP"
                    break
                if c["low"] <= s["target"]:
                    exit_px, exit_ts, why = min(s["target"], c["open"]), c["ts"], "TARGET"
                    break
        if exit_px is None:
            continue                                  # still open at the end of the data
        pts = (exit_px - s["entry"]) if s["side"] == "BUY" else (s["entry"] - exit_px)
        pts -= p["charges_pts"]
        trades.append({"date": b["day"], "entry_time": s["close_time"], "side": s["side"], "level": s["level"],
                       "option": "ATM CE" if s["side"] == "BUY" else "ATM PE", "rsi": s["rsi"],
                       "entry": round(s["entry"], 2), "sl": s["sl"], "target": s["target"],
                       "exit_time": exit_ts[11:16], "exit": round(exit_px, 2), "exit_reason": why,
                       "points": round(pts, 2), "pct": round(pts / s["entry"] * 100, 3)})
        busy_until = j
    return {"summary": summarize(trades, sigs), "trades": trades, "signals": sigs, "params": p}


def summarize(trades, sigs):
    n = len(trades)
    wins = [t for t in trades if t["points"] > 0]
    losses = [t for t in trades if t["points"] <= 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["points"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    skipped = {}
    for s in sigs:
        if not s["taken"]:
            skipped[s["reason"]] = skipped.get(s["reason"], 0) + 1
    by_exit = {}
    for t in trades:
        by_exit[t["exit_reason"]] = by_exit.get(t["exit_reason"], 0) + 1
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "net_points": round(sum(t["points"] for t in trades), 2),
        "avg_win": round(sum(t["points"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["points"] for t in losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown_points": round(dd, 2),
        "days_traded": len({t["date"] for t in trades}),
        "rsi_crosses": len(sigs), "skipped": skipped, "exits": by_exit,
    }


def current_state(candles, params=None):
    """What the rules say right now: today's levels, RSI of the last 15m candle, and any signal on it."""
    p = {**DEFAULTS, **(params or {})}
    bars, rsi, levels, sigs = signals(candles, p)
    if not bars or rsi[-1] is None:
        return None
    last_i = len(bars) - 1
    last = bars[-1]
    lv = levels.get(last["day"])
    sig = next((s for s in reversed(sigs) if s["bar"] >= last_i - 1 and s["ts"][:10] == last["day"]), None)
    price = candles[-1]["close"]
    near = None
    if lv:
        k = min(LEVEL_ORDER, key=lambda k: abs(lv[k] - price))
        near = {"level": k, "value": round(lv[k], 2), "distance": round(price - lv[k], 2)}
    return {
        "day": last["day"], "last_bar": last["ts"], "last_bar_close_time": _hhmm(last["end_min"]),
        "price": price, "rsi": round(rsi[-1], 2), "rsi_prev": round(rsi[-2], 2) if len(rsi) > 1 and rsi[-2] else None,
        "levels": {k: round(lv[k], 2) for k in LEVEL_ORDER} if lv else None,
        "nearest": near, "signal": sig,
    }
