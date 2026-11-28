"""SVIE backtest – three strategies on the same SBC 1-minute candles and capital.

    A  Buy & Hold                 buy ₹20 L at the first candle, hold to the end.
    B  RSI + VWAP (intraday)      RSI7 crosses above 50 with RSI14 > 50 and price
                                  above VWAP. Exits: ladder targets, trailing,
                                  hard stop, close < VWAP or RSI7 < 40, 15:10.
    C  Volume Intelligence        brief sections 5–10: score model + entry gate +
                                  dynamic size (intraday sleeve ₹8 L) and a separate
                                  positional sleeve (₹12 L) that accumulates only on
                                  stabilisation + weakening selling + absorption.

Run (on the server, after `python svie_data.py fetch ...`):

    python svie_backtest.py --symbol-id 6792 --data-from 2026-09-14 \
        --from 2026-09-28 --to 2026-11-27
    python svie_backtest.py --csv data/svie/SBC_1m_2026-09-14_2026-11-27.csv --from 2026-09-28
    python svie_backtest.py ... --sweep        # small parameter search, train/test split

Outputs (in /root/iifl/data/svie/ by default): report HTML, summary JSON,
trades CSV and equity CSV for every strategy.

HONEST DATA LIMITS (printed in every report)
  * IIFL history is 1-minute OHLCV. There is no trade-level aggressor side, so
    "buy volume" is ESTIMATED from where each candle closes in its range
    (close-location value; tick rule when high == low). Volume Delta and Buy
    Ratio in this backtest are therefore estimates, not exchange facts.
  * There is no historical market depth, so bid/ask imbalance, cancellations
    and spoofing-like rules are NOT tested. Record depth from today with
    `svie_data.py record-depth` to test them later.

Execution model (no look-ahead)
  * Signals use only completed candles; orders fill at the NEXT candle's open
    plus slippage. Targets/stops fill intrabar; if a candle touches both, the
    stop is assumed first.
  * Partial fills: every fill is capped at --participation % of that candle's
    volume (shared by all orders in the candle). Entry remainders live for
    3 candles; exit remainders carry until filled (forced at 15:29 if needed).
  * Charges: brokerage per executed fill + STT, exchange, SEBI, stamp, GST
    (rates in lab_backtest.py – verify against your IIFL contract note).
"""

import argparse
import copy
import csv
import json
import math
import os
import sys
from collections import deque
from datetime import date, datetime, timedelta

from lab_backtest import leg_charges

DEFAULTS = {
    "capital": 2_000_000.0,
    "intraday_cap": 800_000.0,
    "positional_cap": 1_200_000.0,
    "base_order": 100_000.0,
    "participation_pct": 10.0,       # max % of a candle's volume we may take
    "slippage_pct": 0.05,            # per fill, against us
    "tick": 0.01,
    "brokerage_per_order": 20.0,     # ₹ per executed ORDER (charged once, even if it fills in parts)
    "last_entry": "15:00",
    "squareoff": "15:10",
    "entry_life_bars": 3,
    "entry_chase_pct": 0.3,          # do not fill an entry > this % above signal close
    "max_entries_per_day": 10,
    "cooldown_bars": 5,
    # risk (operational limits – far inside the ₹10 L absolute boundary)
    "daily_loss_limit": 20_000.0,
    "weekly_loss_limit": 50_000.0,
    "max_drawdown_kill": 200_000.0,
    "absolute_loss_limit": 1_000_000.0,
    # exits (intraday)
    "step_pct": 0.7,                 # ladder step: targets at +1, +2, +3 steps
    "ladder": [0.25, 0.25, 0.25],    # remaining 25% trails
    "trail_pct": 0.5,
    "hard_stop_pct": 1.0,
    # indicators
    "flow_window": 5,                # bars for buy ratio / volume delta
    "rvol_days": 10,                 # comparable-minute history for relative volume
    # strategy B
    "b_rsi_cross": 50.0,
    "b_rsi_exit": 40.0,
    # strategy C – intraday
    "min_score": 50,
    "gate_buy_ratio": 60.0,
    "gate_rvol": 1.5,
    "exit_buy_ratio": 45.0,
    "neg_delta_bars": 3,
    "vwap_exit_buffer_pct": 0.1,
    "exit_votes": 2,                 # exit when >= this many exit conditions agree
    "neg_score_exit": 60,
    # strategy C – positional sleeve
    "pos_tranche": 100_000.0,
    "pos_weak_pct": 3.0,             # price >= this % below the N-day high close
    "pos_lookback_days": 5,
    "pos_window": 30,                # minutes used for stabilisation / absorption
    "pos_min_gap_days": 2,
    "pos_target": 85.0,              # working hypothesis, not a forecast
}

SIZE_TABLE = ((80, 1.00), (65, 0.75), (50, 0.40))   # score >= x -> fraction of base order
NAMES = {"A": "Buy & Hold", "B": "RSI + VWAP", "C": "Volume Intelligence"}
LIMITS_NOTE = ("Buy/sell volume, Volume Delta and Buy Ratio are ESTIMATED from 1-minute candles "
               "(IIFL history has no aggressor side). Market-depth rules (bid/ask imbalance, "
               "cancellations, spoofing-like patterns) are NOT tested – no historical order book.")


# =============================================================== indicators
def rsi_series(closes, period):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period

    def val(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[period] = val(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = val(ag, al)
    return out


def _mins(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


class Prepared:
    """All indicators, computed once from completed data only."""

    def __init__(self, candles, p):
        self.c = candles
        n = len(candles)
        self.n = n
        self.day = [k["ts"][:10] for k in candles]
        self.hhmm = [k["ts"][11:16] for k in candles]
        closes = [k["close"] for k in candles]
        self.rsi7 = rsi_series(closes, 7)
        self.rsi14 = rsi_series(closes, 14)

        # VWAP (resets daily) + estimated aggressor split
        self.vwap = [0.0] * n
        self.buy_vol = [0.0] * n
        pv = vv = 0.0
        for i, k in enumerate(candles):
            if i == 0 or self.day[i] != self.day[i - 1]:
                pv = vv = 0.0
            tp = (k["high"] + k["low"] + k["close"]) / 3
            pv += tp * k["volume"]
            vv += k["volume"]
            self.vwap[i] = pv / vv if vv else k["close"]
            rng = k["high"] - k["low"]
            if rng > 0:
                frac = (k["close"] - k["low"]) / rng
            else:
                prev = closes[i - 1] if i else k["close"]
                frac = 1.0 if k["close"] > prev else 0.0 if k["close"] < prev else 0.5
            self.buy_vol[i] = k["volume"] * frac

        # rolling buy ratio / delta over flow_window bars (same day only)
        w = int(p["flow_window"])
        self.buy_ratio = [50.0] * n
        self.delta = [0.0] * n
        for i in range(n):
            j0 = i
            while j0 > 0 and i - j0 + 1 < w and self.day[j0 - 1] == self.day[i]:
                j0 -= 1
            tv = sum(candles[j]["volume"] for j in range(j0, i + 1))
            bv = sum(self.buy_vol[j] for j in range(j0, i + 1))
            self.buy_ratio[i] = bv / tv * 100 if tv else 50.0
            self.delta[i] = bv - (tv - bv)

        # relative volume vs the same minute on previous days
        hist = {}
        self.rvol = [0.0] * n
        recent = deque(maxlen=20)
        day_buf = []
        for i, k in enumerate(candles):
            if i and self.day[i] != self.day[i - 1]:
                for hm, v in day_buf:
                    hist.setdefault(hm, deque(maxlen=int(p["rvol_days"]))).append(v)
                day_buf = []
            h = hist.get(self.hhmm[i])
            if h and len(h) >= 3:
                avg = sum(h) / len(h)
            else:
                avg = sum(recent) / len(recent) if recent else 0
            self.rvol[i] = k["volume"] / avg if avg > 0 else 0.0
            recent.append(k["volume"])
            day_buf.append((self.hhmm[i], k["volume"]))

        # 5-minute candles; at bar i only the last COMPLETED 5m candle is visible
        self.bull5 = [False] * n
        self.bear5 = [False] * n
        cur = None
        last5 = prev5 = None
        for i, k in enumerate(candles):
            if i and self.day[i] != self.day[i - 1]:
                last5 = prev5 = cur = None
            b = (_mins(self.hhmm[i]) - 555) // 5
            if cur is None or cur["b"] != b:
                cur = {"b": b, "o": k["open"], "h": k["high"], "l": k["low"], "c": k["close"]}
            else:
                cur["h"] = max(cur["h"], k["high"])
                cur["l"] = min(cur["l"], k["low"])
                cur["c"] = k["close"]
            if (_mins(self.hhmm[i]) - 555) % 5 == 4 or self.hhmm[i] == "15:29":
                prev5, last5 = last5, dict(cur)
            if last5:
                ref = prev5["c"] if prev5 else last5["o"]
                self.bull5[i] = last5["c"] > last5["o"] and last5["c"] >= ref
                self.bear5[i] = last5["c"] < last5["o"] and last5["c"] <= ref

        # daily closes for the positional "N-day high"
        self.day_close = {}
        for i in range(n):
            self.day_close[self.day[i]] = closes[i]
        self.days = sorted(self.day_close)
        self.ranges = [k["high"] - k["low"] for k in candles]


# =============================================================== scoring
def buy_score(pr, i):
    s = 0
    if pr.buy_ratio[i] > 65:
        s += 25
    if pr.rvol[i] > 2:
        s += 20
    r, rp = pr.rsi7[i], pr.rsi7[i - 1] if i else None
    if r is not None and rp is not None and 50 <= r <= 70 and r > rp:
        s += 15
    if pr.c[i]["close"] > pr.vwap[i]:
        s += 20
    if pr.bull5[i]:
        s += 20
    return s


def sell_score(pr, i):
    s = 0
    if pr.buy_ratio[i] < 35:
        s += 25
    if pr.rvol[i] > 2 and i and pr.c[i]["close"] < pr.c[i - 1]["close"]:
        s += 20
    r, rp = pr.rsi7[i], pr.rsi7[i - 1] if i else None
    if r is not None and rp is not None and r < 50 and r < rp:
        s += 15
    if pr.c[i]["close"] < pr.vwap[i]:
        s += 20
    if pr.bear5[i]:
        s += 20
    return s


def size_fraction(score):
    for th, f in SIZE_TABLE:
        if score >= th:
            return f
    return 0.0


# =============================================================== simulator
class Lot:
    __slots__ = ("id", "sleeve", "product", "qty", "open_qty", "buy_value", "sell_value", "charges",
                 "entry_px", "entry_ts", "entry_i", "exit_ts", "stop", "targets", "trail", "hi",
                 "reasons", "score")


class Sim:
    def __init__(self, pr, strat, p, a, b):
        self.pr, self.s, self.p, self.a, self.b = pr, strat, p, a, b
        self.cash = p["capital"]
        self.lots, self.closed = [], []
        self.pend_buy, self.pend_sell = [], []
        self.next_id = 1
        self.events = []
        self.equity = []
        self.peak = p["capital"]
        self.max_dd = 0.0
        self.max_exposure = 0.0
        self.util_sum = 0.0
        self.util_n = 0
        self.killed = False
        self.blocked_day = None
        self.blocked_week = None
        self.entries_today = 0
        self.last_entry_i = -10 ** 9
        self.last_pos_day = None
        self.day_realized = 0.0
        self.week_realized = {}
        self.forced = 0
        self.unfilled_entries = 0
        self.fills = 0
        self.fill_log = []          # (bar index, side, qty, price) – used by tests
        self.charged = set()        # orders that already paid brokerage

    # ---------------------------------------------------------------- utils
    def _tick_up(self, x):
        t = self.p["tick"]
        return math.ceil(round(x / t, 6)) * t

    def _tick_dn(self, x):
        t = self.p["tick"]
        return math.floor(round(x / t, 6)) * t

    def _week(self, d):
        y, w, _ = date.fromisoformat(d).isocalendar()
        return f"{y}-W{w}"

    def sleeve_cost(self, sleeve):
        return sum(l.open_qty * l.entry_px for l in self.lots if l.sleeve == sleeve) + \
            sum(o["value"] for o in self.pend_buy if o["sleeve"] == sleeve)

    # ---------------------------------------------------------------- fills
    def _buy_fill(self, i, o, px, cap):
        k = self.pr.c[i]
        qty = int(o["value"] // px)
        qty = min(qty, cap)
        if qty <= 0:
            return 0
        value = qty * px
        brk = 0.0 if o.get("charged") else self.p["brokerage_per_order"]
        ch = leg_charges(value, "BUY", o["product"], brk)
        if value + ch > self.cash:
            qty = int((self.cash - brk - 50) // (px * 1.002))
            if qty <= 0:
                return 0
            value = qty * px
            ch = leg_charges(value, "BUY", o["product"], brk)
        o["charged"] = True
        self.cash -= value + ch
        self.fills += 1
        self.fill_log.append((i, "BUY", qty, px))
        lot = o.get("lot")
        if lot is None:
            lot = Lot()
            lot.id, lot.sleeve, lot.product = self.next_id, o["sleeve"], o["product"]
            self.next_id += 1
            lot.qty = lot.open_qty = 0
            lot.buy_value = lot.sell_value = lot.charges = 0.0
            lot.entry_ts, lot.entry_i, lot.exit_ts = k["ts"], i, None
            lot.trail, lot.reasons, lot.score = False, [], o.get("score")
            lot.targets = []
            self.lots.append(lot)
            o["lot"] = lot
            if lot.sleeve == "INTRA":
                self.entries_today += 1
                self.last_entry_i = i
        lot.qty += qty
        lot.open_qty += qty
        lot.buy_value += value
        lot.charges += ch
        lot.entry_px = lot.buy_value / lot.qty
        lot.hi = max(getattr(lot, "hi", 0) or 0, lot.entry_px)
        self._set_exits(lot)
        o["value"] -= value
        return qty

    def _set_exits(self, lot):
        p = self.p
        if lot.sleeve == "INTRA":
            lot.stop = self._tick_dn(lot.entry_px * (1 - p["hard_stop_pct"] / 100))
            lot.targets, left = [], lot.qty
            for k, frac in enumerate(p["ladder"], 1):
                q = int(lot.qty * frac)
                left -= q
                lot.targets.append([self._tick_up(lot.entry_px * (1 + k * p["step_pct"] / 100)), q])
            # quantities already sold reduce the earliest targets
            sold = lot.qty - lot.open_qty
            for t in lot.targets:
                take = min(sold, t[1])
                t[1] -= take
                sold -= take
        elif lot.sleeve == "POS":
            lot.stop = None
            lot.targets = [[p["pos_target"], lot.qty - (lot.qty - lot.open_qty)]]
        else:
            lot.stop, lot.targets = None, []

    def _sell(self, i, lot, qty, px, reason, order="EXIT"):
        """order: which broker order this fill belongs to – each target level is its own
        limit order; stop / signal / squareoff exits share one exit order per lot."""
        if qty <= 0:
            return 0
        qty = min(qty, lot.open_qty)
        value = qty * px
        key = (lot.id, order)
        brk = 0.0 if key in self.charged else self.p["brokerage_per_order"]
        self.charged.add(key)
        ch = leg_charges(value, "SELL", lot.product, brk)
        self.cash += value - ch
        self.fills += 1
        self.fill_log.append((i, "SELL", qty, px))
        lot.open_qty -= qty
        lot.sell_value += value
        lot.charges += ch
        if reason not in lot.reasons:
            lot.reasons.append(reason)
        if lot.open_qty == 0:
            lot.exit_ts = self.pr.c[i]["ts"]
            self._close(lot)
        return qty

    def _close(self, lot):
        self.lots.remove(lot)
        self.pend_sell = [o for o in self.pend_sell if o["lot"] is not lot]
        gross = lot.sell_value - lot.buy_value
        net = gross - lot.charges
        if lot.sleeve == "INTRA":
            self.day_realized += net
            wk = self._week(lot.exit_ts[:10])
            self.week_realized[wk] = self.week_realized.get(wk, 0) + net
        t0 = datetime.strptime(lot.entry_ts, "%Y-%m-%d %H:%M")
        t1 = datetime.strptime(lot.exit_ts, "%Y-%m-%d %H:%M")
        self.closed.append({
            "id": lot.id, "sleeve": lot.sleeve, "entry_ts": lot.entry_ts, "exit_ts": lot.exit_ts,
            "qty": lot.qty, "avg_buy": round(lot.buy_value / lot.qty, 4),
            "avg_sell": round(lot.sell_value / lot.qty, 4), "gross": round(gross, 2),
            "charges": round(lot.charges, 2), "net": round(net, 2),
            "hold_min": int((t1 - t0).total_seconds() // 60), "exit": " > ".join(lot.reasons),
            "last_exit": lot.reasons[-1] if lot.reasons else "?",
            "score": lot.score,
        })

    def queue_sell(self, lot, reason, market=True):
        # an exit cancels whatever is left of that lot's entry order
        self.pend_buy = [o for o in self.pend_buy if o.get("lot") is not lot]
        for o in self.pend_sell:
            if o["lot"] is lot:
                return
        self.pend_sell.append({"lot": lot, "reason": reason})

    # ---------------------------------------------------------------- bar
    def bar(self, i):
        pr, p = self.pr, self.p
        k = pr.c[i]
        hm = pr.hhmm[i]
        new_day = i == self.a or pr.day[i] != pr.day[i - 1]
        if new_day:
            self.entries_today = 0
            self.day_realized = 0.0
            self.pend_buy = [o for o in self.pend_buy if o["sleeve"] == "BH"]
        cap = int(k["volume"] * p["participation_pct"] / 100)

        # 1. squareoff intraday
        if hm >= p["squareoff"]:
            for lot in [l for l in self.lots if l.sleeve == "INTRA"]:
                self.queue_sell(lot, "SQUAREOFF")

        # 2. pending sells at open
        slip = p["slippage_pct"] / 100
        for o in list(self.pend_sell):
            lot = o["lot"]
            if lot not in self.lots or cap <= 0:
                continue
            q = min(lot.open_qty, cap)
            cap -= self._sell(i, lot, q, self._tick_dn(k["open"] * (1 - slip)), o["reason"])

        # 3. pending buys at open
        for o in list(self.pend_buy):
            if o.get("lot") is not None and o["lot"] not in self.lots:
                self.pend_buy.remove(o)       # lot already exited – never re-buy into it
                continue
            if o["sleeve"] == "INTRA" and hm > p["last_entry"]:
                self.pend_buy.remove(o)
                continue
            px = self._tick_up(k["open"] * (1 + slip))
            if o.get("max_px") and px > o["max_px"]:
                o["life"] -= 1
            elif cap > 0:
                cap -= self._buy_fill(i, o, px, cap)
                o["life"] -= 1
            else:
                o["life"] -= 1
            if o["value"] < px or (o["life"] <= 0 and o["sleeve"] != "BH"):
                if o.get("lot") is None:
                    self.unfilled_entries += 1
                self.pend_buy.remove(o)

        # 4. intrabar stops then targets (only lots that existed before this bar)
        for lot in list(self.lots):
            if lot.entry_i >= i or lot not in self.lots:
                continue
            if any(o["lot"] is lot for o in self.pend_sell):
                continue
            stop = lot.stop
            if lot.trail:
                stop = max(stop or 0, self._tick_dn(lot.hi * (1 - p["trail_pct"] / 100)))
            if stop and k["low"] <= stop and cap > 0:
                px = self._tick_dn(min(k["open"], stop) * (1 - slip))
                got = self._sell(i, lot, min(lot.open_qty, cap), px, "TRAIL" if lot.trail else "STOP")
                cap -= got
                if lot in self.lots:
                    self.queue_sell(lot, "TRAIL" if lot.trail else "STOP")
                continue
            for t in lot.targets:
                if t[1] > 0 and k["high"] >= t[0] and cap > 0 and lot in self.lots:
                    px = max(t[0], k["open"]) if k["open"] > t[0] else t[0]
                    q = min(t[1], cap, lot.open_qty)
                    got = self._sell(i, lot, q, px, "TARGET", order=("T", t[0]))
                    t[1] -= got
                    cap -= got
                    if lot.sleeve == "INTRA":
                        lot.trail = True
        for lot in self.lots:
            lot.hi = max(lot.hi, k["high"])

        # 5. force-close intraday leftovers on the last bar of the day
        last_of_day = i == self.b - 1 or pr.day[i + 1] != pr.day[i]
        if last_of_day:
            for lot in [l for l in self.lots if l.sleeve == "INTRA"]:
                self.forced += 1
                self._sell(i, lot, lot.open_qty, self._tick_dn(k["close"] * (1 - 2 * slip)), "FORCED_EOD")

        # 6. mark to market + risk
        exposure = sum(l.open_qty * k["close"] for l in self.lots)
        eq = self.cash + exposure
        self.equity.append((k["ts"], round(eq, 2)))
        self.peak = max(self.peak, eq)
        self.max_dd = max(self.max_dd, self.peak - eq)
        self.max_exposure = max(self.max_exposure, exposure)
        self.util_sum += sum(l.open_qty * l.entry_px for l in self.lots) / p["capital"]
        self.util_n += 1
        if not self.killed and (self.peak - eq >= p["max_drawdown_kill"] or p["capital"] - eq >= p["absolute_loss_limit"]):
            self.killed = True
            self.pend_buy = []
            self.events.append(f"{k['ts']} KILL SWITCH: drawdown ₹{self.peak - eq:,.0f} – no new entries")
            for lot in [l for l in self.lots if l.sleeve == "INTRA"]:
                self.queue_sell(lot, "KILL")
        intra_open = sum(l.open_qty * k["close"] - l.buy_value + l.sell_value - l.charges
                         for l in self.lots if l.sleeve == "INTRA")
        day_pnl = self.day_realized + intra_open
        wk = self._week(pr.day[i])
        if self.blocked_day != pr.day[i] and day_pnl <= -p["daily_loss_limit"]:
            self.blocked_day = pr.day[i]
            self.events.append(f"{k['ts']} DAILY LOSS LIMIT hit (₹{day_pnl:,.0f}) – intraday flat for the day")
            for lot in [l for l in self.lots if l.sleeve == "INTRA"]:
                self.queue_sell(lot, "DAILY_LIMIT")
        if self.blocked_week != wk and self.week_realized.get(wk, 0) + intra_open <= -p["weekly_loss_limit"]:
            self.blocked_week = wk
            self.events.append(f"{k['ts']} WEEKLY LOSS LIMIT hit – no intraday entries until next week")
            for lot in [l for l in self.lots if l.sleeve == "INTRA"]:
                self.queue_sell(lot, "WEEKLY_LIMIT")

        if i >= self.b - 1:
            return
        # 7. signals at the close (orders fill from the next bar)
        if self.s == "A":
            if i == self.a and not self.lots and not self.pend_buy:
                self.pend_buy.append({"sleeve": "BH", "product": "DELIVERY", "value": p["capital"] * 0.995,
                                      "life": 10 ** 9})
            return
        self.intraday_signals(i, hm, last_of_day)
        if self.s == "C" and not last_of_day:
            self.positional_signals(i, hm)

    # ---------------------------------------------------------------- intraday
    def _can_enter(self, i, hm):
        p = self.p
        return (not self.killed and self.blocked_day != self.pr.day[i]
                and self.blocked_week != self._week(self.pr.day[i])
                and hm < p["last_entry"] and _mins(hm) >= 555 + 5
                and self.entries_today < p["max_entries_per_day"]
                and i - self.last_entry_i > p["cooldown_bars"]
                and not any(o["sleeve"] == "INTRA" for o in self.pend_buy))

    def intraday_signals(self, i, hm, last_of_day):
        pr, p = self.pr, self.p
        k = pr.c[i]
        if i == 0:
            return
        r7, r7p, r14 = pr.rsi7[i], pr.rsi7[i - 1], pr.rsi14[i]
        if r7 is None or r7p is None or r14 is None:
            return
        intra = [l for l in self.lots if l.sleeve == "INTRA"]

        # --- exits on close
        for lot in intra:
            if self.s == "B":
                if k["close"] < pr.vwap[i]:
                    self.queue_sell(lot, "VWAP_LOST")
                elif r7 < p["b_rsi_exit"]:
                    self.queue_sell(lot, "RSI_EXIT")
            else:
                votes, why = 0, []
                neg = all(pr.delta[j] < 0 for j in range(max(0, i - p["neg_delta_bars"] + 1), i + 1)
                          if pr.day[j] == pr.day[i]) and pr.delta[i] < 0
                for ok, name in ((neg, "NEG_DELTA"),
                                 (pr.buy_ratio[i] < p["exit_buy_ratio"], "BUY_RATIO"),
                                 (k["close"] < pr.vwap[i] * (1 - p["vwap_exit_buffer_pct"] / 100), "VWAP_LOST"),
                                 (r7 < 50 <= r7p, "RSI_REV")):
                    if ok:
                        votes += 1
                        why.append(name)
                if votes >= p["exit_votes"]:
                    self.queue_sell(lot, "+".join(why))
                elif sell_score(pr, i) >= p["neg_score_exit"]:
                    self.queue_sell(lot, "NEG_SCORE")

        # --- entries
        if last_of_day or not self._can_enter(i, hm):
            return
        if intra and k["close"] <= max(l.entry_px for l in intra):
            return  # add only into strength, never average down intraday
        room = p["intraday_cap"] - self.sleeve_cost("INTRA")
        score = None
        if self.s == "B":
            ok = (k["close"] > pr.vwap[i] and r7p <= p["b_rsi_cross"] < r7 and r14 > 50)
            value = p["base_order"]
        else:
            score = buy_score(pr, i)
            ok = (k["close"] > pr.vwap[i] and pr.buy_ratio[i] > p["gate_buy_ratio"]
                  and pr.rvol[i] > p["gate_rvol"] and r7 > 50 and r7 > r7p
                  and k["close"] > pr.c[i - 1]["close"] and pr.bull5[i]
                  and score >= p["min_score"])
            value = p["base_order"] * size_fraction(score)
        value = min(value, room)
        if ok and value >= k["close"] * 10:
            self.pend_buy.append({"sleeve": "INTRA", "product": "INTRADAY", "value": value, "score": score,
                                  "life": p["entry_life_bars"],
                                  "max_px": k["close"] * (1 + p["entry_chase_pct"] / 100)})

    # ---------------------------------------------------------------- positional
    def positional_signals(self, i, hm):
        pr, p = self.pr, self.p
        if self.killed or (_mins(hm) - 555) % 5 != 4 or _mins(hm) < 555 + 60:
            return
        d = pr.day[i]
        if self.last_pos_day:
            gap = len([x for x in pr.days if self.last_pos_day < x <= d])
            if gap < p["pos_min_gap_days"]:
                return
        if any(o["sleeve"] == "POS" for o in self.pend_buy):
            return
        room = p["positional_cap"] - self.sleeve_cost("POS")
        value = min(p["pos_tranche"], room)
        k = pr.c[i]
        if value < k["close"] * 10:
            return
        # 1. confirmed weakness: well below the recent N-day high close
        prev = [x for x in pr.days if x < d][-int(p["pos_lookback_days"]):]
        if not prev:
            return
        hi = max(pr.day_close[x] for x in prev)
        if k["close"] > hi * (1 - p["pos_weak_pct"] / 100):
            return
        w = int(p["pos_window"])
        if i - 2 * w < 0 or pr.day[i - 2 * w + 1] != d:
            return
        recent = range(i - w + 1, i + 1)
        before = range(i - 2 * w + 1, i - w + 1)
        # 2. stabilisation: no new low in the last window
        if min(pr.c[j]["low"] for j in recent) < min(pr.c[j]["low"] for j in before):
            return
        # 3. selling pressure weakening: estimated buy share >= 50% and positive delta
        tv = sum(pr.c[j]["volume"] for j in recent)
        bv = sum(pr.buy_vol[j] for j in recent)
        if not tv or bv / tv < 0.5:
            return
        # 4. absorption: a heavy-volume candle that did not fall (small range, closes in upper half)
        rs = sorted(pr.ranges[j] for j in recent)
        med = rs[len(rs) // 2]
        absorbed = any(pr.rvol[j] >= 2 and pr.ranges[j] <= med and pr.ranges[j] > 0
                       and (pr.c[j]["close"] - pr.c[j]["low"]) / pr.ranges[j] >= 0.5 for j in recent)
        if not absorbed:
            return
        self.last_pos_day = d
        self.pend_buy.append({"sleeve": "POS", "product": "DELIVERY", "value": value, "life": 5,
                              "max_px": k["close"] * 1.005, "score": None})
        self.events.append(f"{k['ts']} POSITIONAL tranche signal ₹{value:,.0f} @≈{k['close']:.2f}")


# =============================================================== run + metrics
def index_range(pr, d_from, d_to):
    a = next((i for i in range(pr.n) if pr.day[i] >= d_from), pr.n)
    b = next((i for i in range(pr.n) if pr.day[i] > d_to), pr.n)
    return a, b


def simulate(pr, strat, p, a, b):
    sim = Sim(pr, strat, p, a, b)
    for i in range(a, b):
        sim.bar(i)
    return sim, metrics(sim, pr, p, a, b)


def metrics(sim, pr, p, a, b):
    last = pr.c[b - 1]["close"] if b > a else 0
    closed = sim.closed
    gross_closed = sum(t["gross"] for t in closed)
    net_closed = sum(t["net"] for t in closed)
    open_gross = sum(l.open_qty * last - l.buy_value + l.sell_value for l in sim.lots)
    open_charges = sum(l.charges for l in sim.lots)
    exit_charges = sum(leg_charges(l.open_qty * last, "SELL", l.product, p["brokerage_per_order"])
                       for l in sim.lots if l.open_qty)
    final_eq = sim.equity[-1][1] if sim.equity else p["capital"]
    wins = [t for t in closed if t["net"] > 0]
    intra = [t for t in closed if t["sleeve"] == "INTRA"]
    inv = [{"sleeve": l.sleeve, "qty": l.open_qty, "avg_buy": round(l.entry_px, 2), "since": l.entry_ts,
            "mtm": round(l.open_qty * last - l.buy_value + l.sell_value, 2)} for l in sim.lots]
    return {
        "gross_profit": round(gross_closed + open_gross, 2),
        "net_profit": round(final_eq - p["capital"], 2),
        "net_if_liquidated": round(final_eq - p["capital"] - exit_charges, 2),
        "realised_net": round(net_closed, 2),
        "unrealised_mtm": round(open_gross - open_charges, 2),
        "charges_total": round(sum(t["charges"] for t in closed) + open_charges, 2),
        "max_drawdown": round(sim.max_dd, 2),
        "max_drawdown_pct": round(sim.max_dd / p["capital"] * 100, 2),
        "trades": len(closed),
        "intraday_trades": len(intra),
        "win_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "avg_hold_min": round(sum(t["hold_min"] for t in closed) / len(closed), 1) if closed else 0.0,
        "capital_utilisation_pct": round(sim.util_sum / max(sim.util_n, 1) * 100, 2),
        "max_exposure": round(sim.max_exposure, 2),
        "positional_qty_end": sum(x["qty"] for x in inv if x["sleeve"] in ("POS", "BH")),
        "positional_inventory": inv,
        "fills": sim.fills,
        "unfilled_entries": sim.unfilled_entries,
        "forced_eod_exits": sim.forced,
        "kill_switch": sim.killed,
        "last_close": last,
    }


def exit_breakdown(trades):
    """Group closed trades by their final exit reason: how often, and what it earned."""
    out = {}
    for t in trades:
        r = t.get("last_exit") or "?"
        g = out.setdefault(r, {"reason": r, "trades": 0, "net": 0.0, "wins": 0})
        g["trades"] += 1
        g["net"] += t["net"]
        g["wins"] += t["net"] > 0
    return sorted(({**g, "net": round(g["net"], 2)} for g in out.values()), key=lambda g: g["net"])


def run_all(candles, p, d_from, d_to, strategies="ABC"):
    pr = Prepared(candles, p)
    a, b = index_range(pr, d_from, d_to)
    if b - a < 10:
        raise SystemExit(f"Not enough candles between {d_from} and {d_to} ({b - a}).")
    out = {}
    for s in strategies:
        sim, m = simulate(pr, s, p, a, b)
        out[s] = {"name": NAMES[s], "metrics": m, "trades": sim.closed, "equity": sim.equity,
                  "events": sim.events}
    return pr, a, b, out


# =============================================================== sweep
SWEEP = {
    "B": {"step_pct": [0.5, 0.7, 1.0], "hard_stop_pct": [0.7, 1.0, 1.5], "trail_pct": [0.4, 0.7]},
    "C": {"step_pct": [0.5, 0.7, 1.0], "hard_stop_pct": [0.7, 1.0, 1.5], "min_score": [50, 65, 80],
          "exit_votes": [1, 2]},
}


def sweep(candles, p, d_from, d_to, train_frac=0.7, progress=None):
    pr = Prepared(candles, p)
    a, b = index_range(pr, d_from, d_to)
    days = sorted({pr.day[i] for i in range(a, b)})
    split = days[int(len(days) * train_frac)]
    a2, _ = index_range(pr, split, d_to)
    report = {"train": f"{days[0]} .. {pr.day[a2 - 1]}", "test": f"{split} .. {days[-1]}", "results": {}}
    for s, grid in SWEEP.items():
        keys = list(grid)
        combos = [{}]
        for k in keys:
            combos = [dict(c, **{k: v}) for c in combos for v in grid[k]]
        rows = []
        for n, c in enumerate(combos, 1):
            if progress:
                progress(f"Sweep {NAMES[s]}: {n}/{len(combos)}")
            pp = dict(p, **c)
            _, mt = simulate(pr, s, pp, a, a2)
            score = mt["net_if_liquidated"] - 0.5 * mt["max_drawdown"]
            rows.append((score, c, mt))
        rows.sort(key=lambda r: -r[0])
        best = []
        for score, c, mt in rows[:3]:
            _, mtest = simulate(pr, s, dict(p, **c), a2, b)
            best.append({"params": c, "train_net": mt["net_if_liquidated"], "train_dd": mt["max_drawdown"],
                         "train_trades": mt["trades"], "test_net": mtest["net_if_liquidated"],
                         "test_dd": mtest["max_drawdown"], "test_trades": mtest["trades"]})
        report["results"][s] = {"combos": len(combos), "top": best}
    return report


# =============================================================== output
ROWS = [("Gross profit", "gross_profit", "₹"), ("Net profit (after charges, MTM)", "net_profit", "₹"),
        ("Net if liquidated at last close", "net_if_liquidated", "₹"), ("Charges paid", "charges_total", "₹"),
        ("Max drawdown", "max_drawdown", "₹"), ("Max drawdown %", "max_drawdown_pct", "%"),
        ("Winning trades %", "win_pct", "%"), ("Number of trades", "trades", ""),
        ("Avg holding (min)", "avg_hold_min", ""), ("Capital utilisation %", "capital_utilisation_pct", "%"),
        ("Max exposure", "max_exposure", "₹"), ("Positional inventory (shares)", "positional_qty_end", ""),
        ("Entries not filled (liquidity)", "unfilled_entries", ""), ("Forced 15:29 exits", "forced_eod_exits", ""),
        ("Kill switch triggered", "kill_switch", "")]


def _fmt(v, unit):
    if isinstance(v, bool):
        return "YES" if v else "no"
    if unit == "₹":
        return f"₹{v:,.0f}"
    if unit == "%":
        return f"{v:.2f}%"
    return f"{v:,}" if isinstance(v, int) else f"{v:,.1f}"


def print_table(res):
    keys = list(res)
    print("\n" + "Metric".ljust(34) + "".join(NAMES[k].rjust(22) for k in keys))
    print("-" * (34 + 22 * len(keys)))
    for label, key, unit in ROWS:
        print(label.ljust(34) + "".join(_fmt(res[k]["metrics"][key], unit).rjust(22) for k in keys))


def _svg(res, w=900, h=260):
    colors = {"A": "#7a7a7a", "B": "#2f6fdf", "C": "#d9822b"}
    allv = [v for r in res.values() for _, v in r["equity"]]
    if not allv:
        return ""
    lo, hi = min(allv), max(allv)
    hi = hi if hi > lo else lo + 1
    parts = []
    for k, r in res.items():
        eq = r["equity"]
        step = max(1, len(eq) // 600)
        pts = eq[::step]
        xy = " ".join(f"{j / max(len(pts) - 1, 1) * w:.1f},{h - (v - lo) / (hi - lo) * h:.1f}"
                      for j, (_, v) in enumerate(pts))
        parts.append(f'<polyline fill="none" stroke="{colors[k]}" stroke-width="1.6" points="{xy}"/>')
    legend = " ".join(f'<span style="color:{colors[k]}">■ {NAMES[k]}</span>' for k in res)
    return (f'<div>{legend} &nbsp; <small>range ₹{lo:,.0f} – ₹{hi:,.0f}</small></div>'
            f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:{h}px;background:#fafafa;border:1px solid #ddd">'
            + "".join(parts) + "</svg>")


def write_outputs(res, out_dir, meta, sweep_report=None):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    base = os.path.join(out_dir, f"svie_{meta['from']}_{meta['to']}_{stamp}")
    summary = {"meta": meta, "data_limits": LIMITS_NOTE,
               "strategies": {k: {"name": v["name"], "metrics": v["metrics"], "events": v["events"]}
                              for k, v in res.items()}}
    if sweep_report:
        summary["sweep"] = sweep_report
    with open(base + "_summary.json", "w") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    for k, v in res.items():
        with open(f"{base}_{k}_trades.csv", "w", newline="") as f:
            cols = ["id", "sleeve", "entry_ts", "exit_ts", "qty", "avg_buy", "avg_sell", "gross", "charges",
                    "net", "hold_min", "exit", "last_exit", "score"]
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(v["trades"])
        with open(f"{base}_{k}_equity.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "equity"])
            w.writerows(v["equity"])

    keys = list(res)
    th = "".join(f"<th>{NAMES[k]}</th>" for k in keys)
    trs = "".join("<tr><td>" + label + "</td>" + "".join(f"<td>{_fmt(res[k]['metrics'][key], unit)}</td>" for k in keys)
                  + "</tr>" for label, key, unit in ROWS)
    inv = ""
    for k in keys:
        for x in res[k]["metrics"]["positional_inventory"]:
            inv += (f"<tr><td>{NAMES[k]}</td><td>{x['sleeve']}</td><td>{x['qty']:,}</td><td>₹{x['avg_buy']}</td>"
                    f"<td>{x['since']}</td><td>₹{x['mtm']:,.0f}</td></tr>")
    ev = "".join(f"<li><b>{NAMES[k]}</b>: {e}</li>" for k in keys for e in res[k]["events"][:40])
    sw = ""
    if sweep_report:
        sw = f"<h2>Parameter sweep</h2><p>Train {sweep_report['train']} · Test (unseen) {sweep_report['test']}</p>"
        for s, r in sweep_report["results"].items():
            sw += f"<h3>{NAMES[s]} – {r['combos']} combinations, top 3 on train</h3><table><tr><th>Params</th>" \
                  "<th>Train net</th><th>Train DD</th><th>Test net</th><th>Test DD</th><th>Test trades</th></tr>"
            for t in r["top"]:
                sw += (f"<tr><td>{json.dumps(t['params'])}</td><td>₹{t['train_net']:,.0f}</td><td>₹{t['train_dd']:,.0f}</td>"
                       f"<td>₹{t['test_net']:,.0f}</td><td>₹{t['test_dd']:,.0f}</td><td>{t['test_trades']}</td></tr>")
            sw += "</table>"
        sw += "<p><small>Only trust a setting if the TEST result holds up. Two months of data is a short sample.</small></p>"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>SVIE backtest {meta['from']} – {meta['to']}</title>
<style>body{{font-family:system-ui,Arial;margin:24px;color:#222;max-width:1100px}}table{{border-collapse:collapse;margin:8px 0 18px}}
td,th{{border:1px solid #ddd;padding:5px 10px;text-align:right}}td:first-child,th:first-child{{text-align:left}}
th{{background:#f3f3f3}}.warn{{background:#fff6e0;border:1px solid #f0c36d;padding:10px 14px}}</style></head><body>
<h1>SBC Volume Intelligence Engine – backtest</h1>
<p>{meta['symbol']} · {meta['from']} → {meta['to']} · {meta['candles']} one-minute candles · start capital ₹{meta['capital']:,.0f}
· slippage {meta['slippage_pct']}%/fill · participation ≤{meta['participation_pct']}% of candle volume · brokerage ₹{meta['brokerage']}/fill</p>
<div class="warn"><b>Data limits:</b> {LIMITS_NOTE}</div>
<h2>Comparison</h2><table><tr><th>Metric</th>{th}</tr>{trs}</table>
<h2>Equity curve</h2>{_svg(res)}
<h2>Inventory at the end</h2><table><tr><th>Strategy</th><th>Sleeve</th><th>Qty</th><th>Avg buy</th><th>Since</th><th>MTM</th></tr>{inv}</table>
{sw}
<h2>Risk events</h2><ul>{ev or '<li>none</li>'}</ul>
<p><small>Past results on two months of one stock do not show the strategy will be profitable. Paper-trade before going live.</small></p>
</body></html>"""
    with open(base + "_report.html", "w") as f:
        f.write(html)
    return base


def load_candles(args):
    if args.csv:
        import svie_data
        rows = svie_data.read_csv(args.csv)
    else:
        import candles
        rows = candles.load(args.symbol_id, args.data_from or args.date_from, args.date_to)
    return [r for r in rows if (args.data_from or "0000") <= r["ts"][:10] <= args.date_to]


def main(argv=None):
    ap = argparse.ArgumentParser(description="SVIE 3-strategy backtest")
    ap.add_argument("--csv", help="candles CSV from svie_data.py (else read the server DB)")
    ap.add_argument("--symbol-id", default="6792", help="IIFL instrumentId (SBC = 6792)")
    ap.add_argument("--symbol", default="SBC")
    ap.add_argument("--data-from", help="first candle to load (warm-up for RSI / relative volume)")
    ap.add_argument("--from", dest="date_from", default="2026-09-28")
    ap.add_argument("--to", dest="date_to", default="2026-11-27")
    ap.add_argument("--capital", type=float)
    ap.add_argument("--brokerage", type=float, help="₹ per executed fill")
    ap.add_argument("--slippage", type=float, help="%% per fill")
    ap.add_argument("--participation", type=float, help="max %% of candle volume")
    ap.add_argument("--params", help="JSON file overriding DEFAULTS")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--out-dir")
    args = ap.parse_args(argv)

    p = copy.deepcopy(DEFAULTS)
    if args.params:
        with open(args.params) as f:
            p.update(json.load(f))
    for k, v in (("capital", args.capital), ("brokerage_per_order", args.brokerage),
                 ("slippage_pct", args.slippage), ("participation_pct", args.participation)):
        if v is not None:
            p[k] = v
    rows = load_candles(args)
    if not rows:
        raise SystemExit("No candles found. Run: python svie_data.py fetch --symbol SBC --from 2026-09-14 --to 2026-11-27")
    pr, a, b, res = run_all(rows, p, args.date_from, args.date_to)
    print(f"{args.symbol}: {b - a} candles traded ({pr.day[a]} .. {pr.day[b - 1]}), {a} warm-up candles before.")
    print("NOTE: " + LIMITS_NOTE)
    print_table(res)
    sw = None
    if args.sweep:
        print("\nRunning parameter sweep (train 70% / test 30% of days)…")
        sw = sweep(rows, p, args.date_from, args.date_to)
        for s, r in sw["results"].items():
            print(f"\n{NAMES[s]} – top settings on train ({sw['train']}), checked on test ({sw['test']}):")
            for t in r["top"]:
                print(f"  {t['params']}  train ₹{t['train_net']:,.0f}  test ₹{t['test_net']:,.0f} "
                      f"(dd ₹{t['test_dd']:,.0f}, {t['test_trades']} trades)")
    out_dir = args.out_dir
    if not out_dir:
        try:
            import config
            out_dir = os.path.join(config.DATA_DIR, "svie")
        except Exception:
            out_dir = "svie_out"
    meta = {"symbol": args.symbol, "from": pr.day[a], "to": pr.day[b - 1], "candles": b - a,
            "capital": p["capital"], "slippage_pct": p["slippage_pct"], "participation_pct": p["participation_pct"],
            "brokerage": p["brokerage_per_order"], "params": p}
    base = write_outputs(res, out_dir, meta, sw)
    print(f"\nReport: {base}_report.html  (+ _summary.json, _<A|B|C>_trades.csv, _<A|B|C>_equity.csv)")


if __name__ == "__main__":
    sys.exit(main())
