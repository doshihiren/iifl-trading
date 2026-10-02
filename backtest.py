"""Strategy backtest on stored 1-minute candles (Analysis tab).

Rules mirror the live engine (engine.py):
  * Entry at the start of every 1m / 5m / 15m slot (09:15, 09:20, ...),
    filled at that minute's open price.
  * Each entry gets its own target = fill x (1 + target%), rounded UP to the tick.
  * No stop loss. Positions carry over to the next day (DELIVERY).
  * Limits: max open entries, optional capital limit, optional batch buying
    (N buys -> wait for a sell -> re-buy when price falls X% below that sell).
  * An entry is sold when price reaches its target. If a candle OPENS at or
    above the target the sale is at the open (gap up); otherwise at the target.

Conservative choices where 1-minute data cannot tell the order of events:
  * a position bought inside a candle cannot also sell inside that candle;
  * a re-buy cannot trigger in the same candle as the sale that armed it.
"""

from collections import OrderedDict
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TIMEFRAMES = ("1m", "5m", "15m")
TF_MIN = {"1m": 1, "5m": 5, "15m": 15}


def _tick(price, tick, mode):
    p, t = Decimal(str(price)), Decimal(str(tick or 0.05))
    steps = (p / t).to_integral_value(rounding=ROUND_CEILING if mode == "up" else ROUND_FLOOR)
    return float(steps * t)


def target_price(entry, pct, tick):
    return _tick(Decimal(str(entry)) * (1 + Decimal(str(pct)) / 100), tick, "up")


def _minutes(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _slot_start(ts, tf):
    m = _minutes(ts) - (9 * 60 + 15)
    return m >= 0 and m % TF_MIN[tf] == 0


def _hours_between(a, b):
    fa = datetime.strptime(a, "%Y-%m-%d %H:%M")
    fb = datetime.strptime(b, "%Y-%m-%d %H:%M")
    return (fb - fa).total_seconds() / 3600.0


def simulate(candles, p, tf, detail=True):
    """candles: list of dicts ts/open/high/low/close (1m, sorted). p: parameters dict."""
    pct = float(p["target_pct"])
    shares = int(p["shares"])
    max_open = int(p["max_open"])
    cap_limit = float(p["capital_limit"]) if p.get("capital_limit") else None
    batch_size = int(p.get("batch_size") or 0)
    dip = float(p.get("rebuy_dip_pct") or 0)
    tick = float(p.get("tick") or 0.05)
    charge_pct = float(p.get("charges_pct") or 0) / 100.0

    lots = []                    # open positions
    closed = []
    open_cost = 0.0
    open_qty = 0
    realized_net = 0.0
    peak_capital = 0.0
    max_open_seen = 0
    skips = {"max_open": 0, "capital": 0, "batch_wait": 0}
    entries = 0
    batch = {"no": 1, "bought": 0, "state": "BUYING", "ref": None, "trigger": None, "armed_at": None}
    used_keys = set()
    equity_peak = 0.0
    max_dd = 0.0
    days = OrderedDict()
    lot_id = 0

    def day_row(d):
        if d not in days:
            days[d] = {"day": d, "entries": 0, "exits": 0, "gross": 0.0, "net": 0.0}
        return days[d]

    def buy(px, ts, key):
        nonlocal open_cost, open_qty, peak_capital, max_open_seen, entries, lot_id
        if key in used_keys:
            return False
        if batch_size and batch["state"] != "BUYING":
            skips["batch_wait"] += 1
            used_keys.add(key)
            return False
        if len(lots) >= max_open:
            skips["max_open"] += 1
            used_keys.add(key)
            return False
        if cap_limit is not None and open_cost + px * shares > cap_limit + 1e-9:
            skips["capital"] += 1
            used_keys.add(key)
            return False
        used_keys.add(key)
        lot_id += 1
        lots.append({"id": lot_id, "entry_ts": ts, "entry": px, "qty": shares,
                     "target": target_price(px, pct, tick), "batch": batch["no"], "intrabar": False})
        open_cost += px * shares
        open_qty += shares
        entries += 1
        peak_capital = max(peak_capital, open_cost)
        max_open_seen = max(max_open_seen, len(lots))
        day_row(ts[:10])["entries"] += 1
        if batch_size:
            batch["bought"] += 1
            if batch["bought"] >= batch_size:
                batch["state"] = "WAITING_SELL"
        return True

    def sell(lot, px, ts):
        nonlocal open_cost, open_qty, realized_net
        lots.remove(lot)
        open_cost -= lot["entry"] * lot["qty"]
        open_qty -= lot["qty"]
        gross = (px - lot["entry"]) * lot["qty"]
        charges = (lot["entry"] + px) * lot["qty"] * charge_pct
        net = gross - charges
        realized_net += net
        closed.append({**lot, "exit_ts": ts, "exit": px, "gross": round(gross, 2), "charges": round(charges, 2),
                       "net": round(net, 2), "hold_hours": round(_hours_between(lot["entry_ts"], ts), 2)})
        r = day_row(ts[:10])
        r["exits"] += 1
        r["gross"] += gross
        r["net"] += net
        if batch_size and batch["state"] in ("WAITING_SELL", "WAITING_DIP"):
            batch.update(state="WAITING_DIP", ref=px, trigger=_tick(px * (1 - dip / 100.0), tick, "down"),
                         armed_at=ts)

    def new_batch():
        batch.update(no=batch["no"] + 1, bought=0, state="BUYING", ref=None, trigger=None)

    last_close = None
    first_open = candles[0]["open"] if candles else None
    for k in candles:
        ts, o, h, l, c = k["ts"], k["open"], k["high"], k["low"], k["close"]
        slot_id = ts[:11] + _slot_label(ts, tf)
        # 1. gap-up exits at the open
        for lot in [x for x in lots if x["target"] <= o]:
            sell(lot, o, ts)
        # 2. re-buy level reached at the open
        if batch_size and batch["state"] == "WAITING_DIP" and o <= batch["trigger"] and batch["armed_at"] != ts:
            new_batch()
            buy(o, ts, (slot_id, batch["no"]))
        # 3. regular entry at slot start
        if _slot_start(ts, tf):
            buy(o, ts, (slot_id, batch["no"]))
        # 4. re-buy level reached inside the candle
        if batch_size and batch["state"] == "WAITING_DIP" and l <= batch["trigger"] and batch["armed_at"] != ts:
            trigger = batch["trigger"]
            new_batch()
            if buy(trigger, ts, (slot_id, batch["no"])):
                lots[-1]["intrabar"] = True
        # 5. target exits inside the candle (not for positions bought intrabar)
        for lot in [x for x in lots if x["target"] <= h and not (x["intrabar"] and x["entry_ts"] == ts)]:
            sell(lot, lot["target"], ts)
        # equity / drawdown at close
        equity = realized_net + (c * open_qty - open_cost)
        equity_peak = max(equity_peak, equity)
        max_dd = max(max_dd, equity_peak - equity)
        d = day_row(ts[:10])
        d.update(close=c, open_count=len(lots), invested=round(open_cost, 2),
                 unrealized=round(c * open_qty - open_cost, 2), realized_cum=round(realized_net, 2),
                 equity=round(equity, 2))
        last_close = c

    gross_closed = sum(t["gross"] for t in closed)
    charges_closed = sum(t["charges"] for t in closed)
    unreal = (last_close * open_qty - open_cost) if last_close else 0.0
    trading_days = len(days)
    day_nets = [r["net"] for r in days.values()]
    holds = [t["hold_hours"] for t in closed]
    summary = {
        "timeframe": tf,
        "entries": entries,
        "closed": len(closed),
        "gross_pnl": round(gross_closed, 2),
        "charges": round(charges_closed, 2),
        "net_pnl": round(realized_net, 2),
        "open_count": len(lots),
        "open_qty": open_qty,
        "open_invested": round(open_cost, 2),
        "unrealized": round(unreal, 2),
        "total_pnl": round(realized_net + unreal, 2),
        "peak_capital": round(peak_capital, 2),
        "max_open_seen": max_open_seen,
        "return_on_peak_pct": round(100 * realized_net / peak_capital, 3) if peak_capital else 0.0,
        "total_return_on_peak_pct": round(100 * (realized_net + unreal) / peak_capital, 3) if peak_capital else 0.0,
        "avg_profit_per_trade": round(realized_net / len(closed), 2) if closed else 0.0,
        "avg_hold_hours": round(sum(holds) / len(holds), 2) if holds else None,
        "max_hold_hours": round(max(holds), 2) if holds else None,
        "trading_days": trading_days,
        "avg_net_per_day": round(realized_net / trading_days, 2) if trading_days else 0.0,
        "best_day": round(max(day_nets), 2) if day_nets else 0.0,
        "days_with_exits": sum(1 for r in days.values() if r["exits"]),
        "max_drawdown": round(max_dd, 2),
        "skipped_max_open": skips["max_open"],
        "skipped_capital": skips["capital"],
        "skipped_batch_wait": skips["batch_wait"],
        "batches": batch["no"] if batch_size else None,
        "price_change_pct": round(100 * (last_close - first_open) / first_open, 2) if first_open else 0.0,
        "first_price": first_open,
        "last_price": last_close,
        "oldest_open_hours": round(_hours_between(min(x["entry_ts"] for x in lots), candles[-1]["ts"]), 1) if lots else None,
    }
    if not detail:
        return {"summary": summary}

    for r in days.values():
        r["gross"], r["net"] = round(r["gross"], 2), round(r["net"], 2)
    trades = [
        {"entry_ts": t["entry_ts"], "entry": t["entry"], "qty": t["qty"], "target": t["target"],
         "exit_ts": t["exit_ts"], "exit": t["exit"], "net": t["net"], "gross": t["gross"],
         "hold_hours": t["hold_hours"], "status": "Closed"} for t in closed
    ] + [
        {"entry_ts": x["entry_ts"], "entry": x["entry"], "qty": x["qty"], "target": x["target"],
         "exit_ts": None, "exit": None, "net": round((last_close - x["entry"]) * x["qty"], 2), "gross": None,
         "hold_hours": round(_hours_between(x["entry_ts"], candles[-1]["ts"]), 2), "status": "Open"} for x in lots
    ]
    trades.sort(key=lambda t: t["entry_ts"], reverse=True)
    return {"summary": summary, "days": list(days.values()), "trades": trades[:3000],
            "price": _price_series(candles)}


def _slot_label(ts, tf):
    m = _minutes(ts) - (9 * 60 + 15)
    s = 9 * 60 + 15 + (m // TF_MIN[tf]) * TF_MIN[tf]
    return f"{s // 60:02d}:{s % 60:02d}"


def _price_series(candles, points=700):
    if not candles:
        return []
    step = max(1, len(candles) // points)
    out = [{"ts": k["ts"], "close": k["close"]} for k in candles[::step]]
    if out[-1]["ts"] != candles[-1]["ts"]:
        out.append({"ts": candles[-1]["ts"], "close": candles[-1]["close"]})
    return out


def run(candles, params):
    tf = params.get("timeframe", "15m")
    main = simulate(candles, params, tf, detail=True)
    compare = {t: simulate(candles, params, t, detail=False)["summary"] for t in TIMEFRAMES}
    return {"result": main, "compare": compare}
