"""Swing scanner: "200 SMA + RSI(10) mean reversion" on daily candles for every NSE stock.

Rules (from the strategy video, every part adjustable on the page):
  * Trend filter: today's close is above its 200-day simple moving average.
  * Entry signal: RSI(10) closes below 30 (stock pulled back inside an up-trend).
  * Entry: BUY at the next trading day's open.
  * Exit signal: RSI(10) closes above 40, or the trade has been held 10 days.
  * Exit: SELL at the next trading day's open.
  * Optional stop loss (% below entry or ATR multiple), checked against each day's low.

Data: one daily candle per stock per day in `daily_candles`, downloaded from IIFL's
historical-data API by `SwingSyncThread` inside the worker (never in the trading loop):
a one-off history download when requested on the page, then every trading day after
17:30 IST only the new day is added.
"""

import logging
import math
import threading
import time
from array import array
from datetime import date, datetime, timedelta

import candles as candle_util
import db
import market_calendar as cal
from lab_backtest import leg_charges

log = logging.getLogger("iifl.swing")
IST = candle_util.IST

DAILY_SYNC_AT = "17:30"
REQUEST_PAUSE = 0.35          # seconds between IIFL calls
CHUNK_DAYS = 370              # calendar days per history request (split on error)
STATE_KEY = "swing_sync"

DEFAULTS = {
    "sma_len": 200, "rsi_len": 10, "entry_rsi": 30.0, "exit_rsi": 40.0, "max_hold": 10,
    "sl_mode": "none", "sl_value": 0.0,            # none | pct | atr
    "min_price": 50.0, "min_value_cr": 10.0,        # liquidity filter (20-day average traded value, ₹ crore)
    "capital": 100000.0, "max_positions": 5, "brokerage": 0.0, "near_rsi": 35.0,
}


# ---------------------------------------------------------------- universe + data
def build_universe(contracts=None):
    """Every NSE stock in the EQ series from IIFL's contract file -> swing_universe."""
    if contracts is None:
        from instruments import get_contracts
        contracts = get_contracts("NSEEQ")
    rows = []
    for it in contracts:
        ts = str(it.get("tradingSymbol", "")).upper()
        iid = it.get("instrumentId")
        if not ts.endswith("-EQ") or iid in (None, ""):
            continue
        tick = None
        for k in ("tickSize", "tick_size", "ticksize"):
            try:
                if it.get(k) not in (None, "") and float(it[k]) > 0:
                    tick = float(it[k])
                    break
            except (TypeError, ValueError):
                pass
        rows.append((str(iid), ts[:-3], tick))
    now = db.now_utc()
    with db.tx() as c:
        c.executemany("INSERT INTO swing_universe(instrumentId, symbol, tick_size, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(instrumentId) DO UPDATE SET symbol=excluded.symbol, "
                      "tick_size=COALESCE(excluded.tick_size, swing_universe.tick_size)",
                      [(i, s, t, now) for i, s, t in rows])
    return len(rows)


def parse_daily(body):
    out = {}
    for row in candle_util._candle_rows(body):
        k = candle_util._row_to_candle(row)
        if k:
            out[k["ts"][:10]] = k
    return [{**out[d], "d": d} for d in sorted(out)]


def iifl_fetch_daily(instrument_id, d_from, d_to):
    import broker
    import market_data
    status, body = market_data.historical_candles(
        instrument_id, exchange="NSEEQ", timeframe="1d",
        from_date=d_from.strftime("%d-%b-%Y"), to_date=d_to.strftime("%d-%b-%Y"))
    if broker.is_session_error(status, body):
        raise broker.SessionExpired(broker.message_of(body) or f"HTTP {status}")
    if status != 200:
        raise candle_util.CandleFetchError(f"HTTP {status}: {broker.message_of(body) or str(body)[:160]}")
    return parse_daily(body)


def _store(instrument_id, rows, last_day):
    rows = [r for r in rows if r["d"] <= last_day.isoformat()]
    if rows:
        with db.tx() as c:
            c.executemany(
                "INSERT INTO daily_candles(instrumentId, d, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(instrumentId, d) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume",
                [(str(instrument_id), r["d"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows])
    return len(rows)


def _fetch_range(iid, d_from, d_to, fetch, pause):
    """Fetch [d_from, d_to]; a failing range is split in halves (down to ~1 month)."""
    out = []
    a = d_from
    while a <= d_to:
        b = min(d_to, a + timedelta(days=CHUNK_DAYS - 1))
        out.extend(_fetch_chunk(iid, a, b, fetch, pause))
        a = b + timedelta(days=1)
    return out


def _fetch_chunk(iid, a, b, fetch, pause):
    import broker
    try:
        rows = fetch(iid, a, b)
        if pause:
            time.sleep(pause)
        return rows
    except broker.SessionExpired:
        raise
    except Exception:
        if (b - a).days < 31:
            raise
        mid = a + timedelta(days=(b - a).days // 2)
        return _fetch_chunk(iid, a, mid, fetch, pause) + _fetch_chunk(iid, mid + timedelta(days=1), b, fetch, pause)


def get_state():
    return db.get_runtime(STATE_KEY) or {}


def _put_state(**kw):
    st = get_state()
    st.update(kw)
    db.set_runtime(STATE_KEY, st)
    return st


def request_download(years):
    years = max(1, min(int(years or 3), 10))
    return _put_state(requested=True, years=years, state="QUEUED", message="Waiting for the worker to start…")


def run_sync(now, fetch=iifl_fetch_daily, pause=REQUEST_PAUSE, contracts=None, force=False):
    """One pass: history download if requested, otherwise the daily top-up after 17:30."""
    import broker
    st = get_state()
    last_day = candle_util.last_complete_day(now)
    requested = bool(st.get("requested"))
    daily_due = (st.get("auto", True) and st.get("history_done") and cal.is_trading_day(now)
                 and now.strftime("%H:%M") >= DAILY_SYNC_AT and st.get("synced_to") != last_day.isoformat())
    if not (requested or daily_due or force):
        return None
    if requested or not db.scalar("SELECT COUNT(*) FROM swing_universe"):
        n = build_universe(contracts)
        log.info("event=SWING_UNIVERSE stocks=%s", n)
    years = int(st.get("years") or 3)
    start_default = (last_day - timedelta(days=int(years * 365.25))).isoformat()
    stocks = db.rows("SELECT * FROM swing_universe ORDER BY symbol")
    total = len(stocks)
    _put_state(state="RUNNING", requested=False, total=total, done=0, errors=0, started=db.now_utc(),
               message=f"Downloading daily candles for {total} NSE stocks…")
    errors = 0
    for i, s in enumerate(stocks):
        first = s["last_synced_day"]
        d_from = date.fromisoformat(first) + timedelta(days=1) if first else date.fromisoformat(start_default)
        if requested and not first:
            d_from = date.fromisoformat(start_default)
        elif requested and first:
            # A new, longer history request also fills the older part.
            oldest = db.scalar("SELECT MIN(d) FROM daily_candles WHERE instrumentId=?", (s["instrumentId"],))
            if oldest and oldest > start_default:
                d_from = date.fromisoformat(start_default)
        if d_from <= last_day:
            try:
                rows = _fetch_range(s["instrumentId"], d_from, last_day, fetch, pause)
                _store(s["instrumentId"], rows, last_day)
                with db.tx() as c:
                    c.execute("UPDATE swing_universe SET last_synced_day=?, error=NULL, updated_at=? WHERE instrumentId=?",
                              (last_day.isoformat(), db.now_utc(), s["instrumentId"]))
            except broker.SessionExpired:
                _put_state(state="WAITING_LOGIN", requested=True, done=i,
                           message="IIFL login expired – log in on the dashboard and the download continues.")
                return "session"
            except Exception as exc:  # noqa: BLE001
                errors += 1
                with db.tx() as c:
                    c.execute("UPDATE swing_universe SET error=?, updated_at=? WHERE instrumentId=?",
                              (str(exc)[:200], db.now_utc(), s["instrumentId"]))
        if i % 10 == 0 or i == total - 1:
            _put_state(done=i + 1, errors=errors, message=f"{s['symbol']} ({i + 1}/{total})")
    _put_state(state="DONE", done=total, errors=errors, history_done=True, synced_to=last_day.isoformat(),
               finished=db.now_utc(), message=f"Up to date till {last_day:%d %b %Y}")
    clear_cache()
    log.info("event=SWING_SYNC_DONE stocks=%s errors=%s to=%s", total, errors, last_day)
    return "done"


class SwingSyncThread(threading.Thread):
    def __init__(self, interval=30):
        super().__init__(name="swing-sync", daemon=True)
        self.interval = interval
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            try:
                run_sync(datetime.now(IST))
            except Exception:
                log.exception("event=SWING_SYNC_ERROR")
                _put_state(state="ERROR", message="Download stopped by an error; it retries automatically.")
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()


def data_status():
    st = get_state()
    return {
        **st,
        "stocks": db.scalar("SELECT COUNT(*) FROM swing_universe"),
        "with_data": db.scalar("SELECT COUNT(DISTINCT instrumentId) FROM daily_candles"),
        "first_day": db.scalar("SELECT MIN(d) FROM daily_candles"),
        "last_day": db.scalar("SELECT MAX(d) FROM daily_candles"),
        "failed": db.scalar("SELECT COUNT(*) FROM swing_universe WHERE error IS NOT NULL"),
    }


# ---------------------------------------------------------------- indicators
def sma(values, n):
    out = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(closes, n):
    """Wilder RSI (same as TradingView / the Strategy Lab)."""
    out = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l_ = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            gain += g
            loss += l_
            if i == n:
                gain /= n
                loss /= n
            else:
                continue
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l_) / n
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def atr(high, low, close, n=14):
    out = [None] * len(close)
    a = 0.0
    for i in range(len(close)):
        tr = high[i] - low[i] if i == 0 else max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        if i < n:
            a += tr
            if i == n - 1:
                a /= n
                out[i] = a
        else:
            a = (a * (n - 1) + tr) / n
            out[i] = a
    return out


def avg_value(close, volume, n=20):
    vals = [c * v for c, v in zip(close, volume)]
    out = sma(vals, n)
    return out


class Stock:
    """Compact per-stock series with indicators (arrays keep memory low for ~2000 stocks)."""
    __slots__ = ("iid", "symbol", "tick", "days", "pos", "o", "h", "l", "c", "v", "sma", "rsi", "atr", "val")

    def __init__(self, iid, symbol, tick, rows, p):
        self.iid, self.symbol, self.tick = iid, symbol, tick or 0.05
        self.days = [r[0] for r in rows]
        self.pos = {d: i for i, d in enumerate(self.days)}
        o = [r[1] for r in rows]; h = [r[2] for r in rows]; l_ = [r[3] for r in rows]
        c = [r[4] for r in rows]; v = [r[5] or 0 for r in rows]
        nan = float("nan")
        self.o, self.h, self.l, self.c = array("d", o), array("d", h), array("d", l_), array("d", c)
        self.v = array("d", v)
        self.sma = array("d", [nan if x is None else x for x in sma(c, p["sma_len"])])
        self.rsi = array("d", [nan if x is None else x for x in rsi(c, p["rsi_len"])])
        self.atr = array("d", [nan if x is None else x for x in atr(h, l_, c, 14)])
        self.val = array("d", [nan if x is None else x for x in avg_value(c, v, 20)])

    def setup(self, i, p):
        """Entry signal at the close of day i (trade happens at the next open)."""
        s, r = self.sma[i], self.rsi[i]
        if math.isnan(s) or math.isnan(r):
            return False
        return self.c[i] > s and r < p["entry_rsi"] and self.liquid(i, p)

    def liquid(self, i, p):
        val = self.val[i]
        return self.c[i] >= p["min_price"] and not math.isnan(val) and val >= p["min_value_cr"] * 1e7


def load_stocks(p, d_from=None, d_to=None, symbols=None, progress=None):
    """Load daily candles (+warm-up) for all stocks or the given symbols."""
    warm = None
    if d_from:
        warm = (date.fromisoformat(d_from) - timedelta(days=int(p["sma_len"] * 1.6) + 30)).isoformat()
    uni = db.rows("SELECT instrumentId, symbol, tick_size FROM swing_universe ORDER BY symbol")
    if symbols:
        want = {s.strip().upper() for s in symbols if s.strip()}
        uni = [u for u in uni if u["symbol"] in want]
    out = []
    conn = db.conn()
    for k, u in enumerate(uni):
        sql, args = "SELECT d, open, high, low, close, volume FROM daily_candles WHERE instrumentId=?", [u["instrumentId"]]
        if warm:
            sql += " AND d>=?"; args.append(warm)
        if d_to:
            sql += " AND d<=?"; args.append(d_to)
        rows = conn.execute(sql + " ORDER BY d", args).fetchall()
        if len(rows) > p["sma_len"]:
            out.append(Stock(u["instrumentId"], u["symbol"], u["tick_size"], [tuple(r) for r in rows], p))
        if progress and k % 50 == 0:
            progress(k, len(uni))
    return out


_cache = {"key": None, "stocks": None}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.update(key=None, stocks=None)


def cached_stocks(p, progress=None):
    """All stocks, full history, indicators for (sma_len, rsi_len). Cached until new data arrives."""
    key = (db.scalar("SELECT MAX(d) FROM daily_candles"), db.scalar("SELECT COUNT(*) FROM swing_universe"),
           p["sma_len"], p["rsi_len"])
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["stocks"]
    stocks = load_stocks(p, progress=progress)
    with _cache_lock:
        _cache.update(key=key, stocks=stocks)
    return stocks


# ---------------------------------------------------------------- backtest
def _round_tick(x, tick):
    return round(round(x / tick) * tick, 2)


def backtest(stocks, p, d_from=None, d_to=None, progress=None):
    """Portfolio simulation, one shared capital pool, max N positions, equal-weight slots."""
    cal_days = sorted({d for s in stocks for d in s.days if (not d_from or d >= d_from) and (not d_to or d <= d_to)})
    if not cal_days:
        raise ValueError("No daily candles in this period – download data first")
    capital = float(p["capital"])
    cash = capital
    max_pos = max(1, int(p["max_positions"]))
    held = {}            # iid -> position dict
    pending_entry = []   # (rsi, stock) signalled at the previous close
    trades, equity = [], []
    by_iid = {s.iid: s for s in stocks}
    skipped_slots = skipped_cash = 0
    bro = float(p.get("brokerage") or 0)

    def close_pos(pos, s, i, price, reason, day):
        nonlocal cash
        value = price * pos["qty"]
        ch = leg_charges(value, "SELL", "DELIVERY", bro)
        cash += value - ch
        gross = (price - pos["entry"]) * pos["qty"]
        net = gross - pos["buy_charges"] - ch
        risk = (pos["entry"] - pos["sl"]) * pos["qty"] if pos["sl"] else None
        trades.append({
            "symbol": s.symbol, "entry_day": pos["day"], "entry": round(pos["entry"], 2), "qty": pos["qty"],
            "exit_day": day, "exit": round(price, 2), "reason": reason, "days": pos["bars"],
            "gross": round(gross, 2), "charges": round(pos["buy_charges"] + ch, 2), "net": round(net, 2),
            "ret_pct": round((price / pos["entry"] - 1) * 100, 2),
            "r": round(net / risk, 2) if risk else None, "entry_rsi": round(pos["rsi"], 1),
            "sl": round(pos["sl"], 2) if pos["sl"] else None})

    for n, day in enumerate(cal_days):
        # 1) exits at the open (signal from yesterday's close) and stop losses during the day
        for iid in list(held):
            pos = held[iid]
            s = by_iid[iid]
            i = s.pos.get(day)
            if i is None:
                continue
            pos["bars"] += 1
            if pos["pending"]:
                close_pos(pos, s, i, s.o[i], pos["pending"], day)
                del held[iid]
            elif pos["sl"] and s.l[i] <= pos["sl"]:
                close_pos(pos, s, i, min(s.o[i], pos["sl"]), "Stop loss", day)
                del held[iid]
        # 2) entries at the open, most oversold first
        pending_entry.sort(key=lambda x: x[0])
        equity_now = cash + sum(v["last_close"] * v["qty"] for v in held.values())
        for r_sig, s, sig_i in pending_entry:
            i = s.pos.get(day)
            if i is None or s.iid in held:
                continue
            if len(held) >= max_pos:
                skipped_slots += 1
                continue
            px = s.o[i]
            budget = min(cash, equity_now / max_pos)
            qty = int(budget // (px * 1.0012)) if px > 0 else 0
            if qty < 1:
                skipped_cash += 1
                continue
            value = px * qty
            ch = leg_charges(value, "BUY", "DELIVERY", bro)
            cash -= value + ch
            sl = None
            if p["sl_mode"] == "pct" and p["sl_value"] > 0:
                sl = px * (1 - p["sl_value"] / 100)
            elif p["sl_mode"] == "atr" and p["sl_value"] > 0 and not math.isnan(s.atr[sig_i]):
                sl = px - p["sl_value"] * s.atr[sig_i]
            if sl is not None:
                sl = max(_round_tick(sl, s.tick), 0.05)
            held[s.iid] = {"entry": px, "qty": qty, "day": day, "bars": 1, "sl": sl, "pending": None,
                           "buy_charges": ch, "rsi": r_sig, "last_close": px}
            if sl and s.l[i] <= sl:
                close_pos(held[s.iid], s, i, sl, "Stop loss", day)
                del held[s.iid]
        pending_entry = []
        # 3) at the close: exit signals for tomorrow, new entry signals for tomorrow, mark equity
        for iid, pos in held.items():
            s = by_iid[iid]
            i = s.pos.get(day)
            if i is None:
                continue
            pos["last_close"] = s.c[i]
            r = s.rsi[i]
            if not math.isnan(r) and r > p["exit_rsi"]:
                pos["pending"] = f"RSI above {p['exit_rsi']:g}"
            elif p["max_hold"] and pos["bars"] >= p["max_hold"]:
                pos["pending"] = f"{p['max_hold']} days held"
        for s in stocks:
            i = s.pos.get(day)
            if i is not None and s.iid not in held and s.setup(i, p):
                pending_entry.append((s.rsi[i], s, i))
        mtm = sum(v["last_close"] * v["qty"] for v in held.values())
        equity.append({"d": day, "eq": round(cash + mtm, 2), "open": len(held)})
        if progress and n % 20 == 0:
            progress(n, len(cal_days))

    open_pos = [{"symbol": by_iid[k].symbol, "entry_day": v["day"], "entry": round(v["entry"], 2), "qty": v["qty"],
                 "last": round(v["last_close"], 2), "sl": round(v["sl"], 2) if v["sl"] else None,
                 "unrealised": round((v["last_close"] - v["entry"]) * v["qty"], 2),
                 "exit_tomorrow": v["pending"]} for k, v in held.items()]
    return {"summary": summarize(trades, equity, capital, skipped_slots, skipped_cash),
            "equity": equity, "trades": trades, "open": open_pos}


def summarize(trades, equity, capital, skipped_slots=0, skipped_cash=0):
    final = equity[-1]["eq"] if equity else capital
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gw, gl = sum(t["net"] for t in wins), -sum(t["net"] for t in losses)
    peak, mdd = capital, 0.0
    for e in equity:
        peak = max(peak, e["eq"])
        mdd = min(mdd, e["eq"] / peak - 1)
    days = (date.fromisoformat(equity[-1]["d"]) - date.fromisoformat(equity[0]["d"])).days if len(equity) > 1 else 0
    years = days / 365.25 if days else 0
    cagr = ((final / capital) ** (1 / years) - 1) * 100 if years >= 0.25 and final > 0 else None
    rs = [t["r"] for t in trades if t["r"] is not None]
    return {
        "start": equity[0]["d"] if equity else None, "end": equity[-1]["d"] if equity else None,
        "initial_capital": capital, "final_capital": round(final, 2), "total_profit": round(final - capital, 2),
        "total_return_pct": round((final / capital - 1) * 100, 2), "cagr_pct": round(cagr, 2) if cagr is not None else None,
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "max_dd_pct": round(mdd * 100, 2),
        "calmar": round(cagr / abs(mdd * 100), 2) if cagr is not None and mdd < 0 else None,
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "avg_trade": round(sum(t["net"] for t in trades) / len(trades), 2) if trades else 0,
        "avg_win_pct": round(sum(t["ret_pct"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss_pct": round(sum(t["ret_pct"] for t in losses) / len(losses), 2) if losses else 0,
        "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_win_r": round(sum(r for r in rs if r > 0) / max(1, len([r for r in rs if r > 0])), 2) if rs else None,
        "avg_loss_r": round(sum(r for r in rs if r <= 0) / max(1, len([r for r in rs if r <= 0])), 2) if rs else None,
        "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1) if trades else 0,
        "charges": round(sum(t["charges"] for t in trades), 2),
        "skipped_full": skipped_slots, "skipped_cash": skipped_cash,
    }


def stock_history(s, p):
    """This strategy on one stock alone (one position at a time): quick 'has it worked here?' figure."""
    trades, inpos, pend, sl = [], None, None, None
    for i in range(len(s.days)):
        if inpos is not None:
            px = None
            if pend:
                px = s.o[i]
            elif sl and s.l[i] <= sl:
                px = min(s.o[i], sl)
            if px is not None:
                trades.append(px / inpos - 1)
                inpos, pend, sl = None, None, None
                continue
        if inpos is None and pend == "BUY":
            inpos, pend, held = s.o[i], None, 0
            if p["sl_mode"] == "pct" and p["sl_value"] > 0:
                sl = inpos * (1 - p["sl_value"] / 100)
            elif p["sl_mode"] == "atr" and p["sl_value"] > 0 and not math.isnan(s.atr[i - 1]):
                sl = inpos - p["sl_value"] * s.atr[i - 1]
        if inpos is not None:
            held += 1
            r = s.rsi[i]
            if (not math.isnan(r) and r > p["exit_rsi"]) or (p["max_hold"] and held >= p["max_hold"]):
                pend = "SELL"
        elif s.setup(i, p):
            pend = "BUY"
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_pct": None}
    return {"trades": len(trades), "win_rate": round(sum(1 for t in trades if t > 0) / len(trades) * 100),
            "avg_pct": round(sum(trades) / len(trades) * 100, 2)}


# ---------------------------------------------------------------- scanner
def scan(stocks, p):
    """Signals at the latest stored close: BUY at tomorrow's open, plus stocks close to a signal."""
    if not stocks:
        return {"day": None, "buy": [], "near": [], "checked": 0}
    last = max(s.days[-1] for s in stocks)
    buy, near, checked = [], [], 0
    slot = float(p["capital"]) / max(1, int(p["max_positions"]))
    for s in stocks:
        if s.days[-1] != last:
            continue                       # not traded on the latest day (suspended / delisted)
        i = len(s.days) - 1
        if math.isnan(s.sma[i]) or math.isnan(s.rsi[i]) or not s.liquid(i, p):
            continue
        checked += 1
        if s.c[i] <= s.sma[i]:
            continue
        r = s.rsi[i]
        row = {"symbol": s.symbol, "instrumentId": s.iid, "close": round(s.c[i], 2), "sma": round(s.sma[i], 2),
               "above_sma_pct": round((s.c[i] / s.sma[i] - 1) * 100, 1), "rsi": round(r, 1),
               "rsi_prev": round(s.rsi[i - 1], 1) if not math.isnan(s.rsi[i - 1]) else None,
               "value_cr": round(s.val[i] / 1e7, 1), "atr": round(s.atr[i], 2) if not math.isnan(s.atr[i]) else None,
               "qty": int(slot // s.c[i]) if s.c[i] > 0 else 0}
        if p["sl_mode"] == "pct" and p["sl_value"] > 0:
            row["sl"] = round(s.c[i] * (1 - p["sl_value"] / 100), 2)
        elif p["sl_mode"] == "atr" and p["sl_value"] > 0 and row["atr"]:
            row["sl"] = round(s.c[i] - p["sl_value"] * row["atr"], 2)
        if r < p["entry_rsi"]:
            row["history"] = stock_history(s, p)
            buy.append(row)
        elif r < p["near_rsi"]:
            near.append(row)
    buy.sort(key=lambda x: x["rsi"])
    near.sort(key=lambda x: x["rsi"])
    return {"day": last, "buy": buy, "near": near, "checked": checked}


def params(raw):
    p = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k not in DEFAULTS or v in (None, ""):
            continue
        p[k] = v if isinstance(DEFAULTS[k], str) else type(DEFAULTS[k])(float(v))
    if p["sl_mode"] not in ("none", "pct", "atr"):
        p["sl_mode"] = "none"
    p["sma_len"] = max(5, min(int(p["sma_len"]), 400))
    p["rsi_len"] = max(2, min(int(p["rsi_len"]), 50))
    if p["exit_rsi"] <= p["entry_rsi"]:
        raise ValueError("Exit RSI must be above entry RSI")
    return p
