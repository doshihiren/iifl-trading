"""RSI strategy backtest + optimizer (RSI Analysis tab).

Same base rules as backtest.py (buy at every 1m/5m/15m slot, one target per
entry, no stop loss, positions carried overnight, max-open / capital / batch
limits), plus RSI rules. RSI is always Wilder RSI on 1-MINUTE closes,
whatever the entry timeframe.

No look-ahead: a decision at candle i uses RSI of candle i-1 (the last closed
candle). A signal produced at candle i's close is executed at candle i+1's open.

Entry filters (checked at each slot before buying):
  * max_entry_rsi: skip the buy if RSI >= this (price has run up).
  * min_entry_rsi: skip the buy if RSI <= this and not yet turning up (falling knife).

Exit ("ride the winners"), when hold_rsi is set:
  * price reaches the lot's target and RSI >= hold_rsi -> do not sell, RIDE.
    The target becomes a floor: a riding lot is never booked below it
    (except a gap-down open, which the market forces).
  * a riding lot is sold at the next open when RSI >= exit_rsi (momentum peak)
    or RSI < hold_rsi (momentum fading); at the floor if price falls back to
    it; at the cap (entry * (1 + stretch x target%)) if that is reached.
  * price reaches target and RSI < hold_rsi -> sell at target (plain rule).

Risk and volume rules (to keep the worst dip down; all optional):
  * min_gap_pct + ladder_after: the first `ladder_after` lots buy freely as before; after
    that, each extra lot must be at least X% below the LOWEST open lot. Entries spread
    down a fall (a ladder) instead of all lots being bought at one level.
  * trend_days: skip buys while the last close is below the average of the previous
    N days' closing prices (stock in a downtrend).
  * pause_loss_pct: skip buys while the open lots are down more than X% overall.
  * vol_block_mult: skip a buy right after a red 1-minute candle whose volume was
    >= X times the average of the 20 candles before it (heavy selling).
  * vol_climax_mult: book a riding lot at the next open after a green candle with
    volume >= X times average (buying climax / exhaustion).

Pre-bought shares (holdings entered by the user):
  * split into 1-3 parts; part k is sold at the next open after a candle closes
    with RSI >= book_rsi + (k-1) * step AND close >= buy price x (1 + min profit%).
    Never sold below that minimum price.

Conservative assumptions where 1-minute data cannot show the order of events:
  * a lot bought intrabar cannot also sell in that candle;
  * if a riding lot's floor and cap are both inside one candle, the floor (worse) is assumed;
  * a lot that starts riding intrabar and closes back below its floor is sold at the floor.
"""

import itertools
import math
from collections import OrderedDict
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TIMEFRAMES = ("1m", "5m", "15m")
TF_MIN = {"1m": 1, "5m": 5, "15m": 15}
DEFAULT_PERIOD = 14
WARMUP_BARS = 120                     # RSI is not trusted before this many candles
SESSION_START = 9 * 60 + 15

REASONS = {
    "TARGET": "Sold at target",
    "GAP_TARGET": "Gap-up above target",
    "RSI_PEAK": "Booked extra on RSI peak",
    "RSI_FADE": "Sold on RSI fade",
    "FLOOR": "Fell back to target",
    "CAP": "Hit max stretch",
    "VOL_CLIMAX": "Booked on volume climax",
}
VOL_AVG_BARS = 20
RISK_WEIGHTS = {"profit": (0.25, 0.25), "balanced": (0.5, 0.5), "low_dip": (1.5, 1.0)}


# ---------------------------------------------------------------- helpers
def _tick(price, tick, mode):
    p, t = Decimal(str(price)), Decimal(str(tick or 0.05))
    steps = (p / t).to_integral_value(rounding=ROUND_CEILING if mode == "up" else ROUND_FLOOR)
    return float(steps * t)


def target_price(entry, pct, tick):
    return _tick(Decimal(str(entry)) * (1 + Decimal(str(pct)) / 100), tick, "up")


def _minutes(ts):
    return int(ts[11:13]) * 60 + int(ts[14:16])


def _hours_between(a, b):
    fa = datetime.strptime(a, "%Y-%m-%d %H:%M")
    fb = datetime.strptime(b, "%Y-%m-%d %H:%M")
    return (fb - fa).total_seconds() / 3600.0


def rsi_series(closes, period=DEFAULT_PERIOD):
    """Wilder RSI. out[i] = RSI after candle i closed (None until `period` changes exist)."""
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gain += d
        else:
            loss -= d
    ag, al = gain / period, loss / period

    def value(ag, al):
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    out[period] = value(ag, al)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g, l = (d, 0.0) if d > 0 else (0.0, -d)
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        out[i] = value(ag, al)
    return out


def parse_holdings(text):
    """'100 @ 2450.50' / '100 2450' / '100,2450' per line -> [(qty, price)]. Returns (rows, errors)."""
    rows, errors = [], []
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        clean = line.replace("₹", " ").replace("@", " ").replace(",", " ").replace("x", " ").replace("X", " ")
        parts = [p for p in clean.split() if p]
        try:
            qty, price = int(float(parts[0])), float(parts[1])
            if qty <= 0 or price <= 0:
                raise ValueError
            rows.append((qty, price))
        except (IndexError, ValueError):
            errors.append(f"Line {n}: '{line}' – write it as  quantity @ buy price, e.g. 100 @ 2450")
    return rows, errors


class Prepared:
    """Candles + RSI computed once, reused by every simulation."""

    def __init__(self, candles, trade_from=None, period=DEFAULT_PERIOD, warmup=WARMUP_BARS):
        self.c = candles
        self.n = len(candles)
        self.ts = [k["ts"] for k in candles]
        self.o = [float(k["open"]) for k in candles]
        self.h = [float(k["high"]) for k in candles]
        self.l = [float(k["low"]) for k in candles]
        self.cl = [float(k["close"]) for k in candles]
        self.period = period
        self.rsi = rsi_series(self.cl, period)
        first_trade = 0
        if trade_from:
            first_trade = next((i for i, t in enumerate(self.ts) if t[:10] >= trade_from), self.n)
        self.start = max(first_trade, min(self.n, warmup))
        self.minute = [_minutes(t) - SESSION_START for t in self.ts]
        self.days = sorted({t[:10] for t in self.ts[self.start:]})
        # volume: average of the VOL_AVG_BARS candles BEFORE each candle (None until available)
        self.v = [float(k.get("volume") or 0) for k in candles]
        self.has_volume = sum(1 for x in self.v if x > 0) > self.n * 0.5
        self.vavg = [None] * self.n
        run = 0.0
        for i in range(self.n):
            if i >= VOL_AVG_BARS:
                self.vavg[i] = run / VOL_AVG_BARS
                run -= self.v[i - VOL_AVG_BARS]
            run += self.v[i]
        # daily closes for the trend filter
        self.day_of = []
        self.day_close = []
        last_day = None
        for i, t in enumerate(self.ts):
            d = t[:10]
            if d != last_day:
                self.day_close.append(None)
                last_day = d
            self.day_close[-1] = self.cl[i]
            self.day_of.append(len(self.day_close) - 1)
        self._trend = {}

    def trend_ref(self, n_days):
        """Per candle: average close of the n previous trading days (None if not enough history)."""
        if n_days not in self._trend:
            ref_by_day = []
            for k in range(len(self.day_close)):
                prev = self.day_close[max(0, k - n_days):k]
                ref_by_day.append(sum(prev) / n_days if len(prev) == n_days else None)
            self._trend[n_days] = [ref_by_day[d] for d in self.day_of]
        return self._trend[n_days]

    def slice_days(self, days):
        """Index range [a, b) covering the given trading days (contiguous)."""
        if not days:
            return self.start, self.start
        a = next(i for i in range(self.start, self.n) if self.ts[i][:10] >= days[0])
        b = next((i for i in range(a, self.n) if self.ts[i][:10] > days[-1]), self.n)
        return a, b


# ---------------------------------------------------------------- simulation
def simulate(prep, p, tf, a=None, b=None, detail=True, holdings=None):
    """Run the strategy on prep candles [a, b). p = parameter dict (see module doc)."""
    a = prep.start if a is None else max(a, prep.start)
    b = prep.n if b is None else b
    pct = float(p["target_pct"])
    shares = int(p["shares"])
    max_open = int(p["max_open"])
    cap_limit = float(p["capital_limit"]) if p.get("capital_limit") else None
    batch_size = int(p.get("batch_size") or 0)
    dip = float(p.get("rebuy_dip_pct") or 0)
    tick = float(p.get("tick") or 0.05)
    charge_pct = float(p.get("charges_pct") or 0) / 100.0
    max_rsi = p.get("max_entry_rsi")
    min_rsi = p.get("min_entry_rsi")
    hold_rsi = p.get("hold_rsi")
    exit_rsi = p.get("exit_rsi")
    stretch = float(p.get("max_stretch") or 0)
    gap = float(p.get("min_gap_pct") or 0) / 100.0
    ladder_after = int(p.get("ladder_after") or 0)
    trend_days = int(p.get("trend_days") or 0)
    pause_loss = float(p.get("pause_loss_pct") or 0) / 100.0
    vol_block = float(p.get("vol_block_mult") or 0) if prep.has_volume else 0.0
    vol_climax = float(p.get("vol_climax_mult") or 0) if prep.has_volume else 0.0
    trend = prep.trend_ref(trend_days) if trend_days > 0 else None
    V, VA = prep.v, prep.vavg
    if hold_rsi is not None and exit_rsi is None:
        exit_rsi = 101.0
    step = TF_MIN[tf]

    O, H, L, C, TS, R, MIN = prep.o, prep.h, prep.l, prep.cl, prep.ts, prep.rsi, prep.minute

    lots = []
    closed = []
    open_cost = 0.0
    open_qty = 0
    realized = 0.0
    peak_cap = 0.0
    max_open_seen = 0
    skips = {"max_open": 0, "capital": 0, "batch_wait": 0, "rsi_high": 0, "rsi_low": 0,
             "gap": 0, "trend": 0, "loss_pause": 0, "volume": 0}
    entries = 0
    batch = {"no": 1, "bought": 0, "state": "BUYING", "trigger": None, "armed_at": -1}
    equity_peak = 0.0
    max_dd = 0.0
    days = OrderedDict()
    lid = 0
    used = set()
    st = {"dirty": True, "min_target": math.inf, "n_ride": 0, "n_next": 0, "rides": 0}

    def refresh():
        if st["dirty"]:
            st["min_target"] = min((x["target"] for x in lots if not x["ride"]), default=math.inf)
            st["n_ride"] = sum(1 for x in lots if x["ride"])
            st["n_next"] = sum(1 for x in lots if x["sell_next"])
            st["dirty"] = False

    def start_ride(lot):
        lot["ride"] = lot["rode"] = True
        st["rides"] += 1
        st["dirty"] = True

    def day_row(d):
        r = days.get(d)
        if r is None:
            r = days[d] = {"day": d, "entries": 0, "exits": 0, "gross": 0.0, "net": 0.0, "extra": 0.0}
        return r

    def slot_key(i):
        m = MIN[i]
        return (TS[i][:10], (m // step) * step, batch["no"])

    def buy(px, i):
        nonlocal open_cost, open_qty, peak_cap, max_open_seen, entries, lid
        key = slot_key(i)
        if key in used:
            return False
        used.add(key)
        if batch_size and batch["state"] != "BUYING":
            skips["batch_wait"] += 1
            return False
        if len(lots) >= max_open:
            skips["max_open"] += 1
            return False
        if cap_limit is not None and open_cost + px * shares > cap_limit + 1e-9:
            skips["capital"] += 1
            return False
        lid += 1
        tgt = target_price(px, pct, tick)
        cap = target_price(px, pct * stretch, tick) if (hold_rsi is not None and stretch > 1) else None
        st["dirty"] = True
        lots.append({"id": lid, "i": i, "entry": px, "qty": shares, "target": tgt, "cap": cap,
                     "ride": False, "rode": False, "sell_next": None, "intrabar": False})
        open_cost += px * shares
        open_qty += shares
        entries += 1
        peak_cap = max(peak_cap, open_cost)
        max_open_seen = max(max_open_seen, len(lots))
        day_row(TS[i][:10])["entries"] += 1
        if batch_size:
            batch["bought"] += 1
            if batch["bought"] >= batch_size:
                batch["state"] = "WAITING_SELL"
        return True

    def sell(lot, px, i, reason):
        nonlocal open_cost, open_qty, realized
        lots.remove(lot)
        st["dirty"] = True
        open_cost -= lot["entry"] * lot["qty"]
        open_qty -= lot["qty"]
        gross = (px - lot["entry"]) * lot["qty"]
        charges = (lot["entry"] + px) * lot["qty"] * charge_pct
        net = gross - charges
        realized += net
        extra = (px - lot["target"]) * lot["qty"] if lot["rode"] else 0.0
        if detail:
            closed.append((lot, i, px, reason, gross, charges, net, extra))
        else:
            closed.append((None, i, px, reason, 0, 0, net, extra))
        r = day_row(TS[i][:10])
        r["exits"] += 1
        r["gross"] += gross
        r["net"] += net
        r["extra"] += extra
        if batch_size and batch["state"] in ("WAITING_SELL", "WAITING_DIP"):
            batch.update(state="WAITING_DIP", trigger=_tick(px * (1 - dip / 100.0), tick, "down"), armed_at=i)

    def entry_risk_block(i, o):
        """Risk / volume filters for a regular slot entry at candle i's open (uses closed candles only)."""
        if trend is not None and i > 0 and trend[i] is not None and C[i - 1] < trend[i]:
            return "trend"
        if pause_loss and open_qty and (open_cost - C[i - 1] * open_qty) > pause_loss * open_cost:
            return "loss_pause"
        if vol_block and i > 0 and VA[i - 1] and C[i - 1] < O[i - 1] and V[i - 1] >= vol_block * VA[i - 1]:
            return "volume"
        if gap and lots and len(lots) >= ladder_after:
            if o > min(x["entry"] for x in lots) * (1 - gap):
                return "gap"
        return None

    def new_batch():
        batch.update(no=batch["no"] + 1, bought=0, state="BUYING", trigger=None)

    def wants_ride(r):
        return hold_rsi is not None and r is not None and r >= hold_rsi

    # pre-bought holdings
    hold_parts = []
    if holdings:
        hp = holdings
        parts = max(1, min(3, int(hp.get("parts") or 1)))
        for hi_, (q, bp) in enumerate(hp["rows"]):
            base, rem = divmod(q, parts)
            for k in range(parts):
                qty = base + (rem if k == parts - 1 else 0)
                if qty <= 0:
                    continue
                hold_parts.append({"row": hi_, "part": k + 1, "qty": qty, "buy": bp,
                                   "level": float(hp["book_rsi"]) + k * float(hp.get("step") or 5),
                                   "min_px": bp * (1 + float(hp.get("min_profit_pct") or 0) / 100.0),
                                   "sold": None, "signal": False})

    last_close = C[a] if a < b else None
    for i in range(a, b):
        o, h, lo_, c = O[i], H[i], L[i], C[i]
        rp = R[i - 1] if i > 0 else None
        rp2 = R[i - 2] if i > 1 else None
        day = TS[i][:10]

        refresh()
        # 1. orders decided at the previous close execute at this open
        if st["n_next"]:
            for lot in [x for x in lots if x["sell_next"]]:
                sell(lot, o, i, lot["sell_next"])
            refresh()
        for hpart in hold_parts:
            if hpart["signal"] and hpart["sold"] is None:
                hpart["signal"] = False
                if o >= hpart["min_px"]:
                    hpart["sold"] = (i, o)
        # 2. gap opens
        for lot in ([x for x in lots] if (st["n_ride"] or o >= st["min_target"]) else ()):
            if lot["ride"]:
                if o <= lot["target"]:
                    sell(lot, o, i, "FLOOR")
                elif lot["cap"] and o >= lot["cap"]:
                    sell(lot, o, i, "CAP")
            elif lot["target"] <= o:
                if wants_ride(rp):
                    start_ride(lot)
                    if lot["cap"] and o >= lot["cap"]:
                        sell(lot, o, i, "CAP")
                else:
                    sell(lot, o, i, "GAP_TARGET")
        # 3. re-buy level at the open
        if batch_size and batch["state"] == "WAITING_DIP" and o <= batch["trigger"] and batch["armed_at"] != i:
            new_batch()
            buy(o, i)
        # 4. regular entry at slot start (RSI filters use the last CLOSED candle)
        m = MIN[i]
        if m >= 0 and m % step == 0:
            key = slot_key(i)
            if key not in used:
                if max_rsi is not None and rp is not None and rp >= max_rsi:
                    skips["rsi_high"] += 1
                    used.add(key)
                elif min_rsi is not None and rp is not None and rp <= min_rsi and not (rp2 is not None and rp > rp2):
                    skips["rsi_low"] += 1
                    used.add(key)
                else:
                    why = entry_risk_block(i, o)
                    if why:
                        skips[why] += 1
                        used.add(key)
                    else:
                        buy(o, i)
        # 5. re-buy level inside the candle
        if batch_size and batch["state"] == "WAITING_DIP" and lo_ <= batch["trigger"] and batch["armed_at"] != i:
            trig = batch["trigger"]
            new_batch()
            if buy(trig, i):
                lots[-1]["intrabar"] = True
        # 6. intrabar target / floor / cap
        refresh()
        for lot in ([x for x in lots] if (st["n_ride"] or h >= st["min_target"]) else ()):
            if lot["intrabar"] and lot["i"] == i:
                continue
            if lot["ride"]:
                if lo_ <= lot["target"]:
                    sell(lot, lot["target"], i, "FLOOR")
                elif lot["cap"] and h >= lot["cap"]:
                    sell(lot, lot["cap"], i, "CAP")
            elif lot["target"] <= h:
                if wants_ride(rp):
                    start_ride(lot)
                    if c < lot["target"]:
                        sell(lot, lot["target"], i, "FLOOR")
                else:
                    sell(lot, lot["target"], i, "TARGET")
        # 7. at the close: RSI exit signals for riding lots and holdings
        r = R[i]
        refresh()
        if r is not None and st["n_ride"]:
            for lot in lots:
                if lot["ride"] and not lot["sell_next"]:
                    if r >= exit_rsi:
                        lot["sell_next"] = "RSI_PEAK"
                        st["dirty"] = True
                    elif vol_climax and VA[i] and c > o and V[i] >= vol_climax * VA[i]:
                        lot["sell_next"] = "VOL_CLIMAX"
                        st["dirty"] = True
                    elif r < hold_rsi:
                        lot["sell_next"] = "RSI_FADE"
                        st["dirty"] = True
        if r is not None and hold_parts:
            for hpart in hold_parts:
                if hpart["sold"] is None and r >= hpart["level"] and c >= hpart["min_px"]:
                    hpart["signal"] = True
        # equity
        unreal = c * open_qty - open_cost
        equity = realized + unreal
        if equity > equity_peak:
            equity_peak = equity
        if equity_peak - equity > max_dd:
            max_dd = equity_peak - equity
        if detail:
            d = day_row(day)
            d.update(close=c, rsi=round(r, 1) if r is not None else None, open_count=len(lots),
                     invested=round(open_cost, 2), unrealized=round(unreal, 2),
                     realized_cum=round(realized, 2), equity=round(equity, 2))
        else:
            day_row(day)
        last_close = c

    n_days = len(days)
    unreal = (last_close * open_qty - open_cost) if last_close is not None else 0.0
    loss_open = sum(max(0.0, (x["entry"] - last_close) * x["qty"]) for x in lots) if last_close else 0.0
    by_reason = {k: 0 for k in REASONS}
    extra_total = 0.0
    for t in closed:
        by_reason[t[3]] += 1
        extra_total += t[7]
    summary = {
        "timeframe": tf,
        "entries": entries,
        "closed": len(closed),
        "net_pnl": round(realized, 2),
        "open_count": len(lots),
        "open_qty": open_qty,
        "open_invested": round(open_cost, 2),
        "unrealized": round(unreal, 2),
        "open_loss": round(loss_open, 2),
        "total_pnl": round(realized + unreal, 2),
        "peak_capital": round(peak_cap, 2),
        "max_open_seen": max_open_seen,
        "return_on_peak_pct": round(100 * realized / peak_cap, 3) if peak_cap else 0.0,
        "total_return_on_peak_pct": round(100 * (realized + unreal) / peak_cap, 3) if peak_cap else 0.0,
        "avg_profit_per_trade": round(realized / len(closed), 2) if closed else 0.0,
        "trading_days": n_days,
        "avg_net_per_day": round(realized / n_days, 2) if n_days else 0.0,
        "max_drawdown": round(max_dd, 2),
        "skipped_max_open": skips["max_open"],
        "skipped_capital": skips["capital"],
        "skipped_batch_wait": skips["batch_wait"],
        "skipped_rsi_high": skips["rsi_high"],
        "skipped_rsi_low": skips["rsi_low"],
        "skipped_gap": skips["gap"],
        "skipped_trend": skips["trend"],
        "skipped_loss_pause": skips["loss_pause"],
        "skipped_volume": skips["volume"],
        "max_drawdown_pct": round(100 * max_dd / peak_cap, 2) if peak_cap else 0.0,
        "has_volume": prep.has_volume,
        "exits_by_reason": by_reason,
        "extra_from_rsi": round(extra_total, 2),
        "rides": st["rides"],
        "batches": batch["no"] if batch_size else None,
        "first_price": O[a] if a < b else None,
        "last_price": last_close,
        "price_change_pct": round(100 * (last_close - O[a]) / O[a], 2) if a < b and O[a] else 0.0,
        "oldest_open_hours": round(_hours_between(TS[lots[0]["i"]], TS[b - 1]), 1) if lots else None,
    }
    summary["score"] = score(summary, p.get("risk") or "balanced")
    out = {"summary": summary}
    if hold_parts:
        out["holdings"] = _holdings_report(hold_parts, holdings, prep, last_close, charge_pct)
    if not detail:
        return out

    holds = []
    trades = []
    for lot, i, px, reason, gross, charges, net, extra in closed:
        hh = _hours_between(TS[lot["i"]], TS[i])
        holds.append(hh)
        trades.append({"entry_ts": TS[lot["i"]], "entry": lot["entry"], "qty": lot["qty"], "target": lot["target"],
                       "exit_ts": TS[i], "exit": px, "net": round(net, 2), "extra": round(extra, 2),
                       "reason": REASONS[reason], "rsi_at_entry": _r1(R[lot["i"] - 1]),
                       "hold_hours": round(hh, 2), "status": "Closed"})
    for x in lots:
        trades.append({"entry_ts": TS[x["i"]], "entry": x["entry"], "qty": x["qty"], "target": x["target"],
                       "exit_ts": None, "exit": None, "net": round((last_close - x["entry"]) * x["qty"], 2),
                       "extra": None, "reason": "Riding (above target)" if x["ride"] else "Waiting for target",
                       "rsi_at_entry": _r1(R[x["i"] - 1]),
                       "hold_hours": round(_hours_between(TS[x["i"]], TS[b - 1]), 2), "status": "Open"})
    trades.sort(key=lambda t: t["entry_ts"], reverse=True)
    summary["avg_hold_hours"] = round(sum(holds) / len(holds), 2) if holds else None
    summary["max_hold_hours"] = round(max(holds), 2) if holds else None
    for r in days.values():
        r["gross"], r["net"], r["extra"] = round(r["gross"], 2), round(r["net"], 2), round(r["extra"], 2)
    out.update(days=list(days.values()), trades=trades[:3000], price=_series(prep, a, b))
    return out


def _r1(v):
    return round(v, 1) if v is not None else None


def _holdings_report(parts, hp, prep, last_close, charge_pct):
    rows = []
    sold_qty = 0
    cash = 0.0
    cost_sold = 0.0
    total_qty = 0
    total_cost = 0.0
    for x in parts:
        total_qty += x["qty"]
        total_cost += x["qty"] * x["buy"]
        if x["sold"]:
            i, px = x["sold"]
            proceeds = px * x["qty"] * (1 - charge_pct)
            cash += proceeds
            cost_sold += x["qty"] * x["buy"]
            sold_qty += x["qty"]
            rows.append({"part": x["part"], "qty": x["qty"], "buy": x["buy"], "level": round(x["level"], 1),
                         "min_px": round(x["min_px"], 2), "sold_ts": prep.ts[i], "sold_px": px,
                         "rsi": _r1(prep.rsi[i - 1]), "profit": round(proceeds - x["qty"] * x["buy"], 2),
                         "vs_hold": round(proceeds - x["qty"] * (last_close or px), 2)})
        else:
            rows.append({"part": x["part"], "qty": x["qty"], "buy": x["buy"], "level": round(x["level"], 1),
                         "min_px": round(x["min_px"], 2), "sold_ts": None, "sold_px": None, "rsi": None,
                         "profit": None, "vs_hold": None})
    remaining = total_qty - sold_qty
    end_value = cash + remaining * (last_close or 0)
    hold_value = total_qty * (last_close or 0)
    return {"rows": rows, "total_qty": total_qty, "sold_qty": sold_qty, "remaining": remaining,
            "booked_profit": round(cash - cost_sold, 2), "end_value": round(end_value, 2),
            "hold_value": round(hold_value, 2), "vs_just_holding": round(end_value - hold_value, 2),
            "unrealized_remaining": round(remaining * (last_close or 0) - (total_cost - cost_sold), 2),
            "book_rsi": hp["book_rsi"], "parts": hp.get("parts") or 1, "min_profit_pct": hp.get("min_profit_pct") or 0}


def _series(prep, a, b, points=700):
    if a >= b:
        return []
    step = max(1, (b - a) // points)
    idx = list(range(a, b, step))
    if idx[-1] != b - 1:
        idx.append(b - 1)
    return [{"ts": prep.ts[i], "close": prep.cl[i], "rsi": _r1(prep.rsi[i])} for i in idx]


# ---------------------------------------------------------------- scoring
def min_trades(days):
    return max(5, min(20, 2 * days))


def score(s, risk="balanced"):
    """Risk-adjusted score: return on capital minus weighted worst-dip % and open-loss %.
    risk: 'profit' (light penalty), 'balanced', 'low_dip' (heavy penalty on the worst dip)."""
    cap = s["peak_capital"]
    if not cap or s["closed"] < min_trades(s["trading_days"]):
        return None
    w_dd, w_stuck = RISK_WEIGHTS.get(risk, RISK_WEIGHTS["balanced"])
    ret = 100.0 * s["net_pnl"] / cap
    dd = 100.0 * s["max_drawdown"] / cap
    stuck = 100.0 * s["open_loss"] / cap
    return round(ret - w_dd * dd - w_stuck * stuck, 3)


# ---------------------------------------------------------------- optimizer
GRID = {
    "timeframe": list(TIMEFRAMES),
    "target_pct": [0.5, 0.75, 1.0, 1.5, 2.0],
    "max_entry_rsi": [None, 60, 65, 70, 75],
    "hold_rsi": [None, 55, 60, 65],
    "exit_rsi": [70, 75, 80],
}


def grid_combos(grid=GRID):
    out = []
    for tf, tgt, mx, hold in itertools.product(grid["timeframe"], grid["target_pct"], grid["max_entry_rsi"], grid["hold_rsi"]):
        if hold is None:
            out.append({"timeframe": tf, "target_pct": tgt, "max_entry_rsi": mx, "hold_rsi": None, "exit_rsi": None})
        else:
            for ex in grid["exit_rsi"]:
                if ex > hold:
                    out.append({"timeframe": tf, "target_pct": tgt, "max_entry_rsi": mx, "hold_rsi": hold, "exit_rsi": ex})
    return out


RISK_KEYS = ("min_gap_pct", "ladder_after", "trend_days", "pause_loss_pct", "vol_block_mult")
SETTING_KEYS = ("target_pct", "max_entry_rsi", "hold_rsi", "exit_rsi") + RISK_KEYS


def risk_grid(has_volume, max_open):
    ladders = [(0, 0)] + [(g, a) for g in (0.5, 1.0, 2.0)
                          for a in sorted({max(1, max_open // 5), max(1, max_open // 2)})]
    out = []
    for (gap, after), trend, vol in itertools.product(ladders, [0, 5], [0, 2.0] if has_volume else [0]):
        out.append({"min_gap_pct": gap, "ladder_after": after, "trend_days": trend, "pause_loss_pct": 0,
                    "vol_block_mult": vol})
    return out


def dip_options(base_c, max_open):
    """Variants of one setup that trade profit for a smaller worst dip."""
    q = lambda f: max(1, int(round(max_open * f)))
    rows = [("Your chosen setup", {}),
            (f"Max open {q(0.75)} lots", {"max_open": q(0.75)}),
            (f"Max open {q(0.5)} lots", {"max_open": q(0.5)}),
            (f"Max open {q(0.25)} lots", {"max_open": q(0.25)})]
    for g in (0.5, 1.0, 2.0):
        rows.append((f"Ladder: after {q(0.2)} lots, each new lot {g}% below the lowest",
                     {"min_gap_pct": g, "ladder_after": q(0.2)}))
    rows += [("Skip buys below the 5-day average (downtrend)", {"trend_days": 5}),
             ("Pause buys while open lots are down 5%+", {"pause_loss_pct": 5}),
             ("Skip buys after heavy-selling volume (2×)", {"vol_block_mult": 2}),
             (f"Combined: ladder 1% after {q(0.2)} + 5-day trend + volume",
              {"min_gap_pct": 1.0, "ladder_after": q(0.2), "trend_days": 5, "vol_block_mult": 2})]
    out = []
    for label, ch in rows:
        c = dict(base_c)
        c.update(ch)
        out.append((label, c))
    return out


def _group(c):
    return (c["timeframe"], c["target_pct"], c["max_entry_rsi"])


def optimize(prep, base, progress=None, top=5, risk="balanced"):
    """Two-stage grid search with a 70/30 walk-forward check.

    Stage 1: timeframe x target x entry RSI x hold/exit RSI (750 setups).
    Stage 2: the 10 strongest distinct setups x dip-reducing / volume rules.
    Ranked on the first 70% of days, checked on the last 30%, compared with the
    user's current settings (no RSI, no risk rules)."""
    combos = grid_combos()
    rgrid = risk_grid(prep.has_volume, int(base["max_open"]))
    days = prep.days
    split = len(days) >= 10
    train_days = days[: max(1, int(round(len(days) * 0.7)))] if split else days
    test_days = days[len(train_days):] if split else []
    ta, tb = prep.slice_days(train_days)
    va, vb = prep.slice_days(test_days) if split else (None, None)
    total = len(combos) + 10 * len(rgrid) + (60 if split else 30) + 3 + 2 * 12
    done = 0
    plain_base = dict(base, risk=risk, **{k: 0 for k in RISK_KEYS})

    def tick_progress(msg):
        nonlocal done
        done += 1
        if progress and (done % 10 == 0 or done == total):
            progress(done, total, msg)

    def run(c, a, b):
        p = dict(plain_base)
        p.update({k: c[k] for k in SETTING_KEYS + ("max_open",) if k in c})
        return simulate(prep, p, c["timeframe"], a, b, detail=False)["summary"]

    # ---- stage 1: RSI settings
    train = []
    for c in combos:
        c = dict(c, **{k: 0 for k in RISK_KEYS})
        train.append((c, run(c, ta, tb)))
        tick_progress("Stage 1: RSI settings on the first 70% of days")
    ranked = sorted([t for t in train if t[1]["score"] is not None], key=lambda t: -t[1]["score"])
    seeds, groups = [], set()
    for c, s_ in ranked:
        if _group(c) not in groups:
            groups.add(_group(c))
            seeds.append(c)
        if len(seeds) == 10:
            break

    # ---- stage 2: dip-reducing and volume rules on the strongest setups
    cands = list(train)
    for c in seeds:
        for rg in rgrid:
            if not any(rg.values()):
                tick_progress("Stage 2: dip and volume rules")
                continue
            c2 = dict(c, **rg)
            cands.append((c2, run(c2, ta, tb)))
            tick_progress("Stage 2: dip and volume rules")
    ranked = sorted([t for t in cands if t[1]["score"] is not None], key=lambda t: -t[1]["score"])

    # best variant per distinct setup (30), plus the lowest-dip variants of the same setups
    shortlist, groups = [], set()
    for c, s_ in ranked:
        if _group(c) not in groups:
            groups.add(_group(c))
            shortlist.append((c, s_))
        if len(shortlist) == 30:
            break

    def check(c, s_train):
        s_test = run(c, va, vb) if split else None
        tick_progress("Checking on the last 30% of days (unseen)")
        s_full = run(c, prep.start, prep.n)
        tick_progress("Checking on the last 30% of days (unseen)")
        return {"settings": c, "train": _brief(s_train), "test": _brief(s_test) if s_test else None, "full": _brief(s_full)}

    results = [check(c, st_) for c, st_ in shortlist]

    base_c = dict({"timeframe": base.get("timeframe", "15m"), "target_pct": float(base["target_pct"]),
                   "max_entry_rsi": None, "hold_rsi": None, "exit_rsi": None}, **{k: 0 for k in RISK_KEYS})
    baseline = {"settings": base_c, "train": _brief(run(base_c, ta, tb)),
                "test": _brief(run(base_c, va, vb)) if split else None, "full": _brief(run(base_c, prep.start, prep.n))}
    for _ in range(3):
        tick_progress("Comparing with your current settings")

    b_test = (baseline["test"] or {}).get("score")
    for r in results:
        t = r["test"]
        if not split:
            r["verdict"] = "UNVERIFIED"
        elif t and t["score"] is not None and t["score"] > 0 and (b_test is None or t["score"] >= b_test):
            r["verdict"] = "RELIABLE"
        elif t and t["net_pnl"] > 0:
            r["verdict"] = "OK"
        else:
            r["verdict"] = "PAST_ONLY"
        tscore = t["score"] if t and t["score"] is not None else -999
        r["rank_score"] = round(0.5 * r["train"]["score"] + 0.5 * tscore, 3) if split else r["train"]["score"]
    order = {"RELIABLE": 0, "OK": 1, "UNVERIFIED": 1, "PAST_ONLY": 2}
    results.sort(key=lambda r: (order[r["verdict"]], -r["rank_score"]))

    # ways to cut the worst dip, applied to the top setup (full period + unseen days)
    dips = []
    if results:
        best_c = dict(results[0]["settings"])
        for label, c in dip_options(best_c, int(base["max_open"])):
            full = run(c, prep.start, prep.n)
            tick_progress("Measuring ways to cut the worst dip")
            test = run(c, va, vb) if split else None
            tick_progress("Measuring ways to cut the worst dip")
            ratio = round(full["net_pnl"] / full["max_drawdown"], 2) if full["max_drawdown"] > 0 else None
            dips.append({"label": label, "settings": c, "full": _brief(full), "test": _brief(test) if test else None,
                         "profit_per_dip": ratio})

    by_tf = {}
    for c, s_ in cands:
        if s_["score"] is None:
            continue
        if c["timeframe"] not in by_tf or s_["score"] > by_tf[c["timeframe"]][1]["score"]:
            by_tf[c["timeframe"]] = (c, s_)
    tf_table = {tf: {"settings": v[0], "train": _brief(v[1])} for tf, v in by_tf.items()}

    return {"top": results[:top], "dip_options": dips, "baseline": baseline, "by_timeframe": tf_table,
            "tested": len(cands), "risk": risk, "has_volume": prep.has_volume,
            "split": {"train_from": train_days[0] if train_days else None, "train_to": train_days[-1] if train_days else None,
                      "test_from": test_days[0] if test_days else None, "test_to": test_days[-1] if test_days else None,
                      "enabled": split},
            "min_trades": min_trades(len(train_days))}


def _brief(s):
    keys = ("net_pnl", "return_on_peak_pct", "max_drawdown", "max_drawdown_pct", "open_count", "open_loss", "closed",
            "entries", "peak_capital", "extra_from_rsi", "score", "total_pnl", "trading_days", "skipped_rsi_high",
            "skipped_rsi_low", "skipped_gap", "skipped_trend", "skipped_loss_pause", "skipped_volume")
    return {k: s.get(k) for k in keys}


HOLD_GRID = {"book_rsi": [60, 65, 70, 75, 80, 85], "parts": [1, 2, 3]}


def optimize_holdings(prep, holdings, base):
    """Which RSI level / number of parts would have sold the user's shares best."""
    out = []
    p = dict(base)
    p.update(max_open=0)                            # the strategy itself stays idle
    for level, parts in itertools.product(HOLD_GRID["book_rsi"], HOLD_GRID["parts"]):
        hp = dict(holdings, book_rsi=level, parts=parts)
        rep = simulate(prep, p, "15m", detail=False, holdings=hp)["holdings"]
        out.append({"book_rsi": level, "parts": parts, "sold_qty": rep["sold_qty"], "remaining": rep["remaining"],
                    "booked_profit": rep["booked_profit"], "end_value": rep["end_value"],
                    "vs_just_holding": rep["vs_just_holding"]})
    out.sort(key=lambda r: (-r["end_value"], -r["booked_profit"]))
    return out


def run(candles, params, trade_from=None, holdings=None):
    """Single detailed run + quick 1m/5m/15m comparison."""
    prep = Prepared(candles, trade_from, period=int(params.get("rsi_period") or DEFAULT_PERIOD))
    tf = params.get("timeframe", "15m")
    main = simulate(prep, params, tf, detail=True, holdings=holdings)
    compare = {t: simulate(prep, params, t, detail=False)["summary"] for t in TIMEFRAMES}
    no_rsi = dict(params, max_entry_rsi=None, min_entry_rsi=None, hold_rsi=None, exit_rsi=None,
                  vol_climax_mult=0, **{k: 0 for k in RISK_KEYS})
    plain = simulate(prep, no_rsi, tf, detail=False)["summary"]
    return {"result": main, "compare": compare, "plain": plain, "warmup_from": prep.ts[0] if prep.n else None,
            "trade_from": prep.ts[prep.start] if prep.start < prep.n else None}


def current_signal(candles, params, holdings=None, period=DEFAULT_PERIOD):
    """What the rules say right now, from the latest candles."""
    if len(candles) < period + 2:
        return None
    closes = [float(k["close"]) for k in candles]
    r = rsi_series(closes, period)
    now, prev = r[-1], r[-2]
    price = closes[-1]
    max_rsi, min_rsi = params.get("max_entry_rsi"), params.get("min_entry_rsi")
    vols = [float(k.get("volume") or 0) for k in candles]
    has_vol = sum(1 for x in vols[-200:] if x > 0) > min(200, len(vols)) * 0.5
    vol_avg = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else None
    vol_ratio = round(vols[-1] / vol_avg, 2) if has_vol and vol_avg else None
    trend_days = int(params.get("trend_days") or 0)
    trend_ref = None
    if trend_days:
        day_close = OrderedDict()
        for k in candles:
            day_close[k["ts"][:10]] = float(k["close"])
        prev_days = list(day_close.values())[:-1][-trend_days:]
        if len(prev_days) == trend_days:
            trend_ref = sum(prev_days) / trend_days
    vb = float(params.get("vol_block_mult") or 0)
    if max_rsi is not None and now >= max_rsi:
        entry = ("BLOCKED", f"RSI {now:.1f} ≥ {max_rsi} – too high, skip new buys")
    elif min_rsi is not None and now <= min_rsi and not now > prev:
        entry = ("BLOCKED", f"RSI {now:.1f} ≤ {min_rsi} and not turning up yet – wait")
    elif trend_ref is not None and price < trend_ref:
        entry = ("BLOCKED", f"Price ₹{price:.2f} below its {trend_days}-day average ₹{trend_ref:.2f} – downtrend")
    elif vb and vol_ratio is not None and closes[-1] < float(candles[-1]["open"]) and vol_ratio >= vb:
        entry = ("BLOCKED", f"Heavy selling: red candle with {vol_ratio}× average volume")
    else:
        entry = ("OK", f"RSI {now:.1f} – entries allowed")
    hold_msgs = []
    if holdings and holdings.get("rows"):
        parts = max(1, min(3, int(holdings.get("parts") or 1)))
        step = float(holdings.get("step") or 5)
        mp = float(holdings.get("min_profit_pct") or 0)
        for q, bp in holdings["rows"]:
            min_px = bp * (1 + mp / 100)
            base, rem = divmod(q, parts)
            for k in range(parts):
                qty = base + (rem if k == parts - 1 else 0)
                level = float(holdings["book_rsi"]) + k * step
                ready = now >= level and price >= min_px
                why = []
                if now < level:
                    why.append(f"RSI {now:.1f} < {level:g}")
                if price < min_px:
                    why.append(f"price ₹{price:.2f} < min ₹{min_px:.2f}")
                hold_msgs.append({"qty": qty, "buy": bp, "part": k + 1, "level": level, "min_px": round(min_px, 2),
                                  "action": "SELL NOW" if ready else "HOLD",
                                  "why": "RSI and price both above your levels" if ready else " • ".join(why),
                                  "profit_now": round((price - bp) * qty, 2)})
    return {"ts": candles[-1]["ts"], "price": price, "rsi": round(now, 1), "rsi_prev": round(prev, 1),
            "trend": "rising" if now > prev else ("falling" if now < prev else "flat"),
            "entry": {"state": entry[0], "msg": entry[1]}, "holdings": hold_msgs,
            "volume": vols[-1] if has_vol else None, "volume_ratio": vol_ratio,
            "trend_ref": round(trend_ref, 2) if trend_ref else None, "trend_days": trend_days,
            "recent": [{"ts": candles[i]["ts"], "close": closes[i], "rsi": _r1(r[i]), "volume": vols[i]}
                       for i in range(max(0, len(candles) - 120), len(candles))]}
