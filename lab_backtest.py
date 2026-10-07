"""Strategy Lab backtest engine (separate from backtest.py and rsi_backtest.py).

Four strategies run on the same stored 1-minute candles:

  A  Core ladder (DELIVERY)  buy only when price is X% below the last open buy
                             (+ optional RSI filter), each lot sells at its own
                             target, optional permanent core quantity, no stop.
  B  RSI bounce (INTRADAY)   RSI crosses up through a level (+ optional "above
                             VWAP"), exit at target / stop / RSI exit level / 15:10.
  C  Volume breakout         close above the opening-range high with volume
     (INTRADAY)              >= N x average; stop at range low or %, target = R x risk.
  D  Scalp (INTRADAY)        buy every bar, small target, stop, 15:10 exit.
  E  RSI + Volume            pure RSI + volume rules. Mode "dip": RSI crosses back
     (INTRADAY or DELIVERY)  up through a low level (e.g. 30) on >= N x average
                             volume; exit at target / stop / RSI >= exit level.
                             Mode "momentum": RSI crosses up through a high level
                             (e.g. 60) on >= N x volume; exit when RSI falls back
                             below the exit level (e.g. 50), target or stop.
                             Cool-down after a stop loss, max trades per day.

Mechanics shared by all strategies
  * Signals are decided when a 1m/5m/15m bar CLOSES and filled at the next
    1-minute candle's OPEN (no look-ahead).
  * Stops/targets are checked on every 1-minute candle. A gap through a level
    fills at the open. If one candle touches both stop and target, the STOP is
    assumed first (conservative).
  * Daily loss limit: when today's P&L (booked + open) falls to -limit, intraday
    positions are closed at the next open and no new entries are taken that day.
  * Charges: approximate NSE equity statutory charges + brokerage per order.
"""

import math
from datetime import datetime

TF_MIN = {"1m": 1, "5m": 5, "15m": 15}
STRATEGIES = ("A", "B", "C", "D", "E")
NAMES = {"A": "Core ladder", "B": "RSI bounce", "C": "Volume breakout", "D": "Scalp", "E": "RSI + Volume"}
INTRADAY = {"A": False, "B": True, "C": True, "D": True, "E": True}

# Approximate NSE equity cash charges (verify against your contract note).
CHARGE_RATES = {
    "DELIVERY": {"stt_buy": 0.001, "stt_sell": 0.001, "stamp_buy": 0.00015},
    "INTRADAY": {"stt_buy": 0.0, "stt_sell": 0.00025, "stamp_buy": 0.00003},
}
EXCH_TXN = 0.0000297
SEBI_FEE = 0.000001
GST = 0.18


def leg_charges(value, side, product, brokerage_per_order, custom_pct=None):
    """Charges for one order (buy or sell) of `value` rupees."""
    if custom_pct is not None:
        return value * custom_pct / 100.0
    r = CHARGE_RATES[product]
    stt = value * (r["stt_buy"] if side == "BUY" else r["stt_sell"])
    stamp = value * r["stamp_buy"] if side == "BUY" else 0.0
    exch = value * EXCH_TXN
    sebi = value * SEBI_FEE
    gst = GST * (brokerage_per_order + exch + sebi)
    return stt + stamp + exch + sebi + gst + brokerage_per_order


def _mins(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def _hours(a, b):
    return (datetime.strptime(b, "%Y-%m-%d %H:%M") - datetime.strptime(a, "%Y-%m-%d %H:%M")).total_seconds() / 3600


def _tick_floor(x, tick):
    return math.floor(round(x / tick, 6)) * tick


def _tick_ceil(x, tick):
    return math.ceil(round(x / tick, 6)) * tick


# ---------------------------------------------------------------- indicators
class Prepared:
    """1m candles + tf bars with RSI, VWAP, average volume and opening range."""

    def __init__(self, candles, tf, rsi_period=14, vol_lookback=20, orb_minutes=15):
        self.c = candles
        self.tf = tf
        self.n = len(candles)
        self.day = [k["ts"][:10] for k in candles]
        self.minute = [_mins(k["ts"]) for k in candles]
        step = TF_MIN[tf]
        start = 9 * 60 + 15
        # Build tf bars; remember the 1m index that closes each bar.
        bars = []
        cur = None
        for i, k in enumerate(candles):
            slot = (self.day[i], (self.minute[i] - start) // step)
            if cur is None or cur["key"] != slot:
                cur = {"key": slot, "o": k["open"], "h": k["high"], "l": k["low"], "c": k["close"],
                       "v": k["volume"], "pv": ((k["high"] + k["low"] + k["close"]) / 3) * k["volume"],
                       "end": i, "day": self.day[i]}
                bars.append(cur)
            else:
                cur["h"] = max(cur["h"], k["high"])
                cur["l"] = min(cur["l"], k["low"])
                cur["c"] = k["close"]
                cur["v"] += k["volume"]
                cur["pv"] += ((k["high"] + k["low"] + k["close"]) / 3) * k["volume"]
                cur["end"] = i
        self.bars = bars
        self.bar_at_end = {b["end"]: j for j, b in enumerate(bars)}
        self.has_volume = any(k["volume"] for k in candles)
        # RSI (Wilder) on bar closes, continuous across days.
        rsi = [None] * len(bars)
        gain = loss = 0.0
        for j in range(1, len(bars)):
            ch = bars[j]["c"] - bars[j - 1]["c"]
            g, l_ = max(ch, 0.0), max(-ch, 0.0)
            if j <= rsi_period:
                gain += g
                loss += l_
                if j == rsi_period:
                    gain /= rsi_period
                    loss /= rsi_period
                    rsi[j] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
            else:
                gain = (gain * (rsi_period - 1) + g) / rsi_period
                loss = (loss * (rsi_period - 1) + l_) / rsi_period
                rsi[j] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        self.rsi = rsi
        # VWAP per day (falls back to average close when volume is missing).
        vwap = []
        d0, cpv, cv, csum, cn = None, 0.0, 0.0, 0.0, 0
        for b in bars:
            if b["day"] != d0:
                d0, cpv, cv, csum, cn = b["day"], 0.0, 0.0, 0.0, 0
            cpv += b["pv"]
            cv += b["v"]
            csum += b["c"]
            cn += 1
            vwap.append(cpv / cv if cv else csum / cn)
        self.vwap = vwap
        # Average volume of the previous `vol_lookback` bars.
        avgv = [None] * len(bars)
        run = 0.0
        for j, b in enumerate(bars):
            if j >= vol_lookback:
                avgv[j] = run / vol_lookback
                run -= bars[j - vol_lookback]["v"]
            run += b["v"]
        self.avgvol = avgv
        # Opening range per day from 1m candles.
        self.orb = {}
        orb_end = start + orb_minutes
        for i, k in enumerate(candles):
            if self.minute[i] < orb_end:
                d = self.day[i]
                hi, lo = self.orb.get(d, (k["high"], k["low"]))
                self.orb[d] = (max(hi, k["high"]), min(lo, k["low"]))
        self.orb_end = orb_end


def liquidity(candles):
    if not candles:
        return {}
    vols = sorted(k["volume"] for k in candles)
    days = {}
    for k in candles:
        d = days.setdefault(k["ts"][:10], {"v": 0, "h": k["high"], "l": k["low"], "o": k["open"]})
        d["v"] += k["volume"]
        d["h"] = max(d["h"], k["high"])
        d["l"] = min(d["l"], k["low"])
    ranges = [100 * (d["h"] - d["l"]) / d["o"] for d in days.values() if d["o"]]
    med = vols[len(vols) // 2]
    return {
        "price": candles[-1]["close"],
        "days": len(days),
        "avg_daily_volume": round(sum(d["v"] for d in days.values()) / len(days)),
        "median_1m_volume": med,
        "avg_1m_volume": round(sum(vols) / len(vols)),
        "avg_day_range_pct": round(sum(ranges) / len(ranges), 2) if ranges else 0,
        "safe_order_qty": int(med * 0.10),
        "has_volume": any(vols),
    }


# ---------------------------------------------------------------- simulation
def _qty(p, entry, stop, free_capital):
    if p.get("sizing") == "risk" and stop is not None and entry > stop:
        q = int(p["risk_per_trade"] // (entry - stop))
    else:
        q = int(p["shares"])
    if free_capital is not None:
        q = min(q, int(max(free_capital, 0) // entry))
    return max(q, 0)


def simulate(prep, strat, p, g, a=0, b=None, detail=True):
    """Run strategy `strat` ('A'..'D') with params p and global settings g on candles [a, b]."""
    c, bars = prep.c, prep.bars
    b = prep.n - 1 if b is None else b
    intraday = INTRADAY[strat]
    if strat == "E":
        intraday = p.get("product", "INTRADAY") != "DELIVERY"
    product = "INTRADAY" if intraday else "DELIVERY"
    bucket = float(g["core_capital"] if strat == "A" else g["active_capital"])
    tick = float(g.get("tick") or 0.05)
    brok = float(g.get("brokerage") or 0)
    custom = g.get("charges_pct")
    loss_limit = float(g.get("daily_loss_limit") or 0)
    last_entry = _hhmm(g.get("last_entry_time", "15:00"))
    square_off = _hhmm(g.get("square_off_time", "15:10"))
    max_pos = int(p.get("max_positions") or 1)

    pos = []                 # open positions
    trades = []
    realized = 0.0
    charges_total = 0.0
    pending = None           # ("BUY", signal info) to fill at next open
    pending_exit = None      # reason to exit all positions at the next open
    day = None
    day_start_equity = 0.0
    day_blocked = False
    day_trades = 0
    limit_days = 0
    last_buy = None
    equity_peak = 0.0
    max_dd = 0.0
    peak_cap = 0.0
    cap_sum = 0.0
    cap_n = 0
    daily = []
    core = None
    last_stop_i = None       # 1m index of the last stop-loss exit (E cool-down)
    sig = {"rsi_cross": 0, "vol_ok": 0, "blocked_cooldown": 0, "blocked_day_max": 0, "blocked_busy": 0}

    def used():
        return sum(x["entry"] * x["qty"] for x in pos)

    def open_value(px):
        return sum((px - x["entry"]) * x["qty"] for x in pos)

    def buy(i, price, stop=None, target=None, info=None):
        nonlocal charges_total, last_buy, day_trades, peak_cap
        free = bucket - used()
        q = _qty(p, price, stop, free)
        if q <= 0:
            return False
        ch = leg_charges(price * q, "BUY", product, brok, custom)
        charges_total += ch
        pos.append({"entry": price, "qty": q, "stop": stop, "target": target, "i": i, "ts": c[i]["ts"],
                    "buy_ch": ch, "high": price, "info": info or {}})
        last_buy = price
        day_trades += 1
        peak_cap = max(peak_cap, used())
        return True

    def sell(x, i, price, reason):
        nonlocal realized, charges_total, last_stop_i
        if reason.startswith("Stop"):
            last_stop_i = i
        pos.remove(x)
        ch = leg_charges(price * x["qty"], "SELL", product, brok, custom)
        charges_total += ch
        gross = (price - x["entry"]) * x["qty"]
        net = gross - x["buy_ch"] - ch
        realized += net
        trades.append({"entry_ts": x["ts"], "entry": round(x["entry"], 2), "qty": x["qty"],
                       "exit_ts": c[i]["ts"], "exit": round(price, 2), "gross": round(gross, 2),
                       "charges": round(x["buy_ch"] + ch, 2), "net": round(net, 2), "reason": reason,
                       "hold_h": round(_hours(x["ts"], c[i]["ts"]), 2), **x["info"]})

    def equity(px):
        """Booked profit (after all charges) + open positions (after their buy charges) + core."""
        e = realized + open_value(px) - sum(x["buy_ch"] for x in pos)
        if core:
            e += (px - core["entry"]) * core["qty"] - core["ch"]
        return e

    for i in range(a, b + 1):
        k = c[i]
        d = prep.day[i]
        m = prep.minute[i]
        o, h, l_, cl = k["open"], k["high"], k["low"], k["close"]

        # ---- new day
        if d != day:
            if day is not None:
                daily[-1]["close"] = c[i - 1]["close"]
            day = d
            day_start_equity = equity(o)
            day_blocked = False
            day_trades = 0
            daily.append({"day": d, "trades": 0, "net": 0.0, "equity": 0.0, "open": 0, "used": 0.0})
            if strat == "A" and core is None and int(p.get("core_qty") or 0) > 0:
                q = int(p["core_qty"])
                ch = leg_charges(o * q, "BUY", "DELIVERY", brok, custom)
                charges_total += ch
                core = {"qty": q, "entry": o, "ch": ch, "ts": k["ts"]}

        # ---- pending orders at the open
        if pending_exit and pos:
            for x in list(pos):
                sell(x, i, o, pending_exit)
        pending_exit = None
        if intraday and m >= square_off and pos:
            for x in list(pos):
                sell(x, i, o, "Square-off 15:10")
        if pending is not None:
            kind, info = pending
            pending = None
            if not day_blocked and not (intraday and m >= last_entry) and len(pos) < max_pos:
                stop = target = None
                if strat == "A":
                    target = _tick_ceil(o * (1 + p["target_pct"] / 100), tick)
                    if p.get("sl_pct"):
                        stop = _tick_floor(o * (1 - p["sl_pct"] / 100), tick)
                elif strat == "C":
                    if p.get("sl_mode") == "orb" and info.get("orb_low") and info["orb_low"] < o:
                        stop = info["orb_low"]
                    else:
                        stop = _tick_floor(o * (1 - p["sl_pct"] / 100), tick)
                    target = _tick_ceil(o + p["r_multiple"] * (o - stop), tick)
                else:
                    stop = _tick_floor(o * (1 - p["sl_pct"] / 100), tick) if p.get("sl_pct") else None
                    target = _tick_ceil(o * (1 + p["target_pct"] / 100), tick) if p.get("target_pct") else None
                buy(i, o, stop, target, info=info.get("sig"))

        # ---- stops / targets on this 1-minute candle
        for x in list(pos):
            st, tg = x["stop"], x["target"]
            if st is not None and o <= st:
                sell(x, i, o, "Stop (gap)")
            elif tg is not None and o >= tg:
                sell(x, i, o, "Target (gap)")
            elif st is not None and l_ <= st:
                sell(x, i, st, "Stop")
            elif tg is not None and h >= tg:
                sell(x, i, tg, "Target")
            else:
                if p.get("trail_pct") and st is not None:
                    x["high"] = max(x["high"], h)
                    x["stop"] = max(st, _tick_floor(x["high"] * (1 - p["trail_pct"] / 100), tick))

        # ---- daily loss limit
        eq = equity(cl)
        if loss_limit and not day_blocked and eq - day_start_equity <= -loss_limit:
            day_blocked = True
            limit_days += 1
            if intraday and pos:
                pending_exit = "Daily loss limit"

        # ---- signals at bar close
        j = prep.bar_at_end.get(i)
        nxt_same_day = i + 1 <= b and prep.day[i + 1] == d
        if j is not None and not day_blocked and (nxt_same_day or not intraday):
            bar = bars[j]
            r, r_prev = prep.rsi[j], prep.rsi[j - 1] if j else None
            want = False
            info = {}
            if strat == "A":
                spacing_ok = last_buy is None or not pos or bar["c"] <= last_buy * (1 - p["spacing_pct"] / 100)
                rsi_ok = p.get("rsi_max") in (None, "") or (r is not None and r <= p["rsi_max"])
                want = spacing_ok and rsi_ok and len(pos) < max_pos
            elif strat == "B":
                if pos and p.get("rsi_exit") and r is not None and r >= p["rsi_exit"]:
                    pending_exit = pending_exit or "RSI exit"
                cross = r is not None and r_prev is not None and r_prev < p["rsi_buy"] <= r
                vwap_ok = not p.get("vwap_filter") or bar["c"] >= prep.vwap[j]
                want = cross and vwap_ok and not pos
            elif strat == "C":
                rng = prep.orb.get(d)
                after = prep.minute[i] + 1 >= prep.orb_end
                vol_ok = (not prep.has_volume) or (prep.avgvol[j] and bar["v"] >= p["vol_mult"] * prep.avgvol[j])
                want = (rng is not None and after and bar["c"] > rng[0] and vol_ok and not pos
                        and day_trades < int(p.get("max_trades_day") or 1))
                info = {"orb_low": rng[1] if rng else None}
            elif strat == "E":
                level = p["rsi_level"]
                ex = p.get("rsi_exit")
                if pos and ex not in (None, "") and r is not None:
                    if p.get("mode") == "momentum" and r < ex:
                        pending_exit = pending_exit or "RSI faded"
                    elif p.get("mode") != "momentum" and r >= ex:
                        pending_exit = pending_exit or "RSI exit"
                cross = r is not None and r_prev is not None and r_prev < level <= r
                ratio = (bar["v"] / prep.avgvol[j]) if (prep.has_volume and prep.avgvol[j]) else None
                vol_ok = (not prep.has_volume) or (ratio is not None and ratio >= p["vol_mult"])
                if cross:
                    sig["rsi_cross"] += 1
                    if vol_ok:
                        sig["vol_ok"] += 1
                cool = p.get("cooldown_bars") or 0
                cool_ok = last_stop_i is None or prep.day[last_stop_i] != d or \
                    (i - last_stop_i) >= cool * TF_MIN[prep.tf]
                want = cross and vol_ok
                if want and not cool_ok:
                    sig["blocked_cooldown"] += 1
                    want = False
                if want and day_trades >= int(p.get("max_trades_day") or 99):
                    sig["blocked_day_max"] += 1
                    want = False
                if want and len(pos) >= max_pos:
                    sig["blocked_busy"] += 1
                    want = False
                info = {"sig": {"rsi": round(r, 1), "volx": round(ratio, 2) if ratio is not None else None}} if want else {}
            else:  # D
                want = len(pos) < max_pos
            if want and (not intraday or prep.minute[i] + 1 < last_entry):
                pending = ("BUY", info)

        # ---- bookkeeping at close
        eq = equity(cl)
        equity_peak = max(equity_peak, eq)
        max_dd = max(max_dd, equity_peak - eq)
        u = used() + (core["entry"] * core["qty"] if core else 0)
        peak_cap = max(peak_cap, u)
        cap_sum += u
        cap_n += 1
        daily[-1].update(equity=round(eq, 2), open=len(pos), used=round(u, 2))

    # ---- results
    last_close = c[b]["close"] if b >= a else 0
    if daily:
        daily[-1]["close"] = last_close
    prev = 0.0
    tdays = {}
    for t in trades:
        tdays.setdefault(t["exit_ts"][:10], []).append(t["net"])
    for r in daily:
        r["net"] = round(sum(tdays.get(r["day"], [])), 2)
        r["trades"] = len(tdays.get(r["day"], []))
        r["change"] = round(r["equity"] - prev, 2)
        prev = r["equity"]
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    open_pnl = round(open_value(last_close) - sum(x["buy_ch"] for x in pos), 2)
    core_pnl = round((last_close - core["entry"]) * core["qty"] - core["ch"], 2) if core else None
    total = daily[-1]["equity"] if daily else 0.0
    # longest stretch (in trading days) below a previous equity high
    best, run, longest = -1e18, 0, 0
    for r in daily:
        if r["equity"] >= best:
            best, run = r["equity"], 0
        else:
            run += 1
            longest = max(longest, run)
    changes = [r["change"] for r in daily]
    summary = {
        "strategy": strat, "name": NAMES[strat] + (f" ({'momentum' if p.get('mode') == 'momentum' else 'dip'})" if strat == "E" else ""),
        "product": product,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "net_closed": round(sum(nets), 2),
        "charges": round(charges_total, 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else (None if not wins else 99.0),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "open_count": len(pos), "open_qty": sum(x["qty"] for x in pos), "open_pnl": open_pnl,
        "core_qty": core["qty"] if core else 0, "core_pnl": core_pnl,
        "total": round(total, 2),
        "return_pct": round(100 * total / bucket, 2) if bucket else 0.0,
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(100 * max_dd / bucket, 2) if bucket else 0.0,
        "peak_capital": round(peak_cap, 2),
        "avg_capital": round(cap_sum / cap_n, 2) if cap_n else 0.0,
        "bucket": bucket,
        "days": len(daily),
        "green_days": sum(1 for x in changes if x > 0),
        "red_days": sum(1 for x in changes if x < 0),
        "best_day": round(max(changes), 2) if changes else 0.0,
        "worst_day": round(min(changes), 2) if changes else 0.0,
        "longest_dd_days": longest,
        "limit_days": limit_days,
        "signals": sig if strat == "E" else None,
        "avg_hold_h": round(sum(t["hold_h"] for t in trades) / len(trades), 2) if trades else None,
        "from": c[a]["ts"] if b >= a else None, "to": c[b]["ts"] if b >= a else None,
    }
    if not detail:
        return {"summary": summary}
    reasons = {}
    for t in trades:
        reasons.setdefault(t["reason"], [0, 0.0])
        reasons[t["reason"]][0] += 1
        reasons[t["reason"]][1] += t["net"]
    summary["exits"] = {k: {"count": v[0], "net": round(v[1], 2)} for k, v in reasons.items()}
    return {"summary": summary, "daily": daily,
            "trades": list(reversed(trades))[:2000],
            "open": [{"entry_ts": x["ts"], "entry": round(x["entry"], 2), "qty": x["qty"],
                      "target": x["target"], "stop": x["stop"],
                      "pnl": round((last_close - x["entry"]) * x["qty"], 2)} for x in pos]}


# ---------------------------------------------------------------- ranges
def index_range(prep, d_from, d_to):
    """First/last 1m index whose date is within [d_from, d_to]."""
    a = next((i for i, d in enumerate(prep.day) if d >= d_from), None)
    b = next((i for i in range(prep.n - 1, -1, -1) if prep.day[i] <= d_to), None)
    if a is None or b is None or a > b:
        return None
    return a, b


# ---------------------------------------------------------------- optimizer
GRIDS = {
    "A": {"spacing_pct": [0.5, 1, 1.5, 2, 3], "target_pct": [1, 1.5, 2, 3, 5], "rsi_max": [None, 40]},
    "B": {"rsi_buy": [25, 30, 35], "target_pct": [0.5, 0.8, 1.2], "sl_pct": [0.4, 0.7, 1.0], "vwap_filter": [False, True]},
    "C": {"vol_mult": [1.5, 2, 3], "r_multiple": [1, 1.5, 2], "sl_mode": ["orb", "pct"], "trail_pct": [0, 0.5]},
    "D": {"target_pct": [0.2, 0.3, 0.5], "sl_pct": [0.2, 0.3, 0.5], "max_positions": [1, 3, 5]},
    "E": {"rsi_level": [25, 30, 35], "vol_mult": [1.5, 2, 3], "target_pct": [0.8, 1.2, 2], "sl_pct": [0.5, 0.8, 1.2]},
}
E_MOMENTUM_LEVELS = [55, 60, 65]


def combos(strat, base=None):
    grid = dict(GRIDS[strat])
    if strat == "E" and (base or {}).get("mode") == "momentum":
        grid["rsi_level"] = E_MOMENTUM_LEVELS
    keys = list(grid)
    out = [{}]
    for k in keys:
        out = [{**o, k: v} for o in out for v in grid[k]]
    return out


def score(s):
    """Higher is better: total P&L, penalised by drawdown; needs a few trades."""
    if s["trades"] < 3 and s["strategy"] != "A":
        return -1e12
    return s["total"] - 0.5 * s["max_dd"]


def optimize(prep, strat, base, g, train, test, progress=None, top=5):
    results = []
    cs = combos(strat, base)
    for n, cmb in enumerate(cs):
        p = {**base, **cmb}
        s = simulate(prep, strat, p, g, train[0], train[1], detail=False)["summary"]
        results.append((score(s), cmb, s))
        if progress:
            progress(n + 1, len(cs))
    results.sort(key=lambda r: r[0], reverse=True)
    best = []
    for sc, cmb, s in results[:top]:
        t = simulate(prep, strat, {**base, **cmb}, g, test[0], test[1], detail=False)["summary"] if test else None
        best.append({"params": cmb, "train": s, "test": t})
    return {"tested": len(cs), "best": best}
